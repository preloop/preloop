// Audit chain verification (#558).
//
// The point of this command is that it does not trust our answer. The server
// has an endpoint that walks its own chain and reports a verdict, and that
// verdict is worth exactly the trust the caller already places in the server,
// which for a tamper evidence feature is the wrong amount. So this command
// asks for the material (canonical row payloads and stored hashes), recomputes
// every hash locally, and says plainly when its verdict differs from ours.
//
// What a clean walk proves: the rows in the range are in the order they were
// sealed in, none was removed from the middle, and none was edited after
// sealing. What it does not prove: that any row was true when it was written.
// The chain is built by the same platform that writes the rows.

package cmd

import (
	"encoding/json"
	"fmt"
	"sort"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/verify"
)

const (
	auditChainStatusPath      = "/api/v1/audit/chain/status"
	auditChainVerifyPath      = "/api/v1/audit/chain/verify"
	auditChainSegmentPath     = "/api/v1/audit/chain/segment"
	auditChainCheckpointsPath = "/api/v1/audit/chain/checkpoints"
	signingKeysPath           = "/api/v1/signing/keys"

	auditSegmentPageSize = 500
	// auditMaxPages bounds one run at half a million rows. A chain longer
	// than that wants a range, not a bigger default.
	auditMaxPages = 1000
)

// chainStatus is GET /audit/chain/status.
type chainStatus struct {
	Enabled            bool             `json:"enabled"`
	HeadSeq            int64            `json:"head_seq"`
	HeadHash           string           `json:"head_hash"`
	LastSealedAt       string           `json:"last_sealed_at"`
	PrunedBelowSeq     int64            `json:"pruned_below_seq"`
	SealedRows         int64            `json:"sealed_rows"`
	UnsealedRows       int64            `json:"unsealed_rows"`
	SealLagSeconds     int64            `json:"seal_lag_seconds"`
	CheckpointInterval int64            `json:"checkpoint_interval"`
	LatestCheckpoint   *chainCheckpoint `json:"latest_checkpoint"`
	ActiveKeyID        string           `json:"active_key_id"`
}

// chainCheckpoint is one signed anchor over the chain head at a sequence.
type chainCheckpoint struct {
	Seq               int64                     `json:"seq"`
	ChainHash         string                    `json:"chain_hash"`
	RowCount          int64                     `json:"row_count"`
	CheckpointedAt    string                    `json:"checkpointed_at"`
	SigningKeyID      string                    `json:"signing_key_id"`
	Signature         string                    `json:"signature"`
	SignedPayload     map[string]interface{}    `json:"signed_payload"`
	Digest            string                    `json:"digest"`
	SignatureDocument *verify.SignatureDocument `json:"signature_document"`
}

// serverChainVerdict is GET /audit/chain/verify, kept only so a disagreement
// between our answer and the local walk is visible.
type serverChainVerdict struct {
	Status       string `json:"status"`
	CheckedRows  int64  `json:"checked_rows"`
	StartSeq     int64  `json:"start_seq"`
	EndSeq       int64  `json:"end_seq"`
	HeadSeq      int64  `json:"head_seq"`
	UnsealedRows int64  `json:"unsealed_rows"`
	FirstBreak   *struct {
		Kind   string `json:"kind"`
		Seq    int64  `json:"seq"`
		RowID  string `json:"row_id"`
		Detail string `json:"detail"`
	} `json:"first_break"`
}

// auditVerifyReport is the --json shape. It is deliberately explicit about
// scope: a verdict without a range is not a verdict.
type auditVerifyReport struct {
	Status         string                 `json:"status"`
	CheckedRows    int64                  `json:"checked_rows"`
	StartSeq       int64                  `json:"start_seq"`
	EndSeq         int64                  `json:"end_seq"`
	HeadSeq        int64                  `json:"head_seq"`
	PrunedBelowSeq int64                  `json:"pruned_below_seq"`
	UnsealedRows   int64                  `json:"unsealed_rows"`
	FirstBreak     *verify.Break          `json:"first_break,omitempty"`
	Checkpoints    []checkpointVerdict    `json:"checkpoints,omitempty"`
	ServerStatus   string                 `json:"server_status,omitempty"`
	ServerAgrees   bool                   `json:"server_agrees"`
	Proves         map[string]interface{} `json:"proves"`
}

// checkpointVerdict is one anchor checked against the rows in hand.
type checkpointVerdict struct {
	Seq             int64  `json:"seq"`
	KeyID           string `json:"key_id"`
	SignatureOK     bool   `json:"signature_ok"`
	MatchesLocalRow bool   `json:"matches_local_rows"`
	Detail          string `json:"detail,omitempty"`
}

var (
	auditStartSeq int64
	auditEndSeq   int64
	auditJSON     bool
)

// auditCmd is the parent for audit trail commands.
var auditCmd = &cobra.Command{
	Use:   "audit",
	Short: "Inspect and verify the tamper-evident audit trail",
	Long: `Work with the per-account audit hash chain.

Audit rows are chained: each sealed row commits to the row before it, so a
row that was edited or removed after sealing breaks the chain from that point
on. Checkpoints over the chain head are signed with the account's Ed25519 key,
which is what lets a checkpoint you stored last quarter contradict a chain
rewritten today.`,
}

// auditVerifyCmd implements `preloop audit verify`.
var auditVerifyCmd = &cobra.Command{
	Use:   "verify",
	Short: "Walk the audit hash chain locally and report the first break",
	Long: `Fetch the chain material and recompute every hash on this machine.

The command asks the server for each row's canonical payload and the hashes
it stored, then recomputes sha256(row_domain + canonical JSON) itself and
checks that each row commits to the one before it. It reports the first break
and stops there: everything after the first break is a consequence of it.

Signed checkpoints are verified against the account's published public keys,
and each checkpoint is held against the rows fetched now, so a chain rebuilt
after the fact fails against an anchor made before it.

Scope, which is the honest part: rows below the retention purge floor are
gone under a stated policy and are not verified, and rows written since the
last sealing pass are not chained yet. A clean walk proves order and
non-deletion within the range it names. It does not prove any row was true
when it was written: the chain is built by the platform that writes the rows.

Exit status is 1 when the chain is broken, so CI can gate on it.

Examples:
  preloop audit verify
  preloop audit verify --start-seq 1000 --end-seq 2000
  preloop audit verify --json`,
	RunE: runAuditVerify,
}

// auditKeysCmd implements `preloop audit keys`.
var auditKeysCmd = &cobra.Command{
	Use:   "keys",
	Short: "Show the account's signing keys (public halves only)",
	Long: `List every signing key the account has held, newest first.

Retired keys stay listed on purpose: every signature they made is still
valid, and a bundle exported last year needs the key that signed it.

Keep a copy of these somewhere we cannot reach. A public key you fetch from
us at verification time only proves the bundle matches whatever key we serve
you today.`,
	RunE: runAuditKeys,
}

func init() {
	auditVerifyCmd.Flags().Int64Var(&auditStartSeq, "start-seq", 0, "first chain sequence to check (default: the purge floor)")
	auditVerifyCmd.Flags().Int64Var(&auditEndSeq, "end-seq", 0, "last chain sequence to check (default: the chain head)")
	auditVerifyCmd.Flags().BoolVar(&auditJSON, "json", false, "emit the verdict as JSON")
	auditKeysCmd.Flags().BoolVar(&auditJSON, "json", false, "emit the keys as JSON")

	auditCmd.AddCommand(auditVerifyCmd)
	auditCmd.AddCommand(auditKeysCmd)
}

func runAuditVerify(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()

	var status chainStatus
	if err := client.Get(auditChainStatusPath, &status); err != nil {
		return fmt.Errorf("could not read the chain status: %w", err)
	}

	report := auditVerifyReport{
		HeadSeq:        status.HeadSeq,
		PrunedBelowSeq: status.PrunedBelowSeq,
		UnsealedRows:   status.UnsealedRows,
		ServerAgrees:   true,
	}

	// The server's own verdict, fetched first so that a disagreement is
	// reportable. It is not the answer, and it is recorded in the audit trail
	// on their side, which is where a verification belongs.
	var verdict serverChainVerdict
	verdictPath := auditChainVerifyPath
	if query := seqQuery(auditStartSeq, auditEndSeq); query != "" {
		verdictPath += "?" + query
	}
	if err := client.Get(verdictPath, &verdict); err != nil {
		// A server that cannot give its own verdict does not stop a local
		// walk. It is one more thing to report, not a reason to give up.
		fmt.Fprintf(cmd.ErrOrStderr(), "note: the server verdict is unavailable (%v); walking locally anyway\n", err)
	} else {
		report.ServerStatus = verdict.Status
	}

	checkpoints, err := fetchCheckpoints(client, auditStartSeq)
	if err != nil {
		fmt.Fprintf(cmd.ErrOrStderr(), "note: checkpoints are unavailable (%v)\n", err)
	}

	after := auditStartSeq - 1
	if after < status.PrunedBelowSeq {
		after = status.PrunedBelowSeq
	}
	if after < 0 {
		after = 0
	}
	// Empty expectedPrev means "take the first row's word for it", which is
	// honest only for a mid-chain start. From sequence 1 the first row must
	// commit to genesis, matching the server walk and the package tests.
	expectedPrev := ""
	if after < 1 {
		expectedPrev = verify.GenesisHash
	}
	walk := verify.NewChainWalk(verify.RowDomainV1, after, expectedPrev)
	watched := make([]int64, 0, len(checkpoints))
	for _, checkpoint := range checkpoints {
		watched = append(watched, checkpoint.Seq)
	}
	walk.Watch(watched)

	pages := 0
	for {
		segment, err := fetchSegment(client, after)
		if err != nil {
			return fmt.Errorf("could not read the chain: %w", err)
		}
		if segment.RowDomain != "" {
			walk.SetRowDomain(segment.RowDomain)
		}
		entries := trimToEnd(segment.Entries, auditEndSeq)
		walk.Feed(entries)
		if walk.Break != nil {
			break
		}
		after = walk.LastSeq
		if len(entries) < len(segment.Entries) || !segment.HasMore || len(segment.Entries) == 0 {
			break
		}
		pages++
		if pages >= auditMaxPages {
			fmt.Fprintf(cmd.ErrOrStderr(),
				"note: stopping after %d pages; narrow the range with --start-seq and --end-seq\n",
				pages)
			break
		}
	}

	report.CheckedRows = walk.Checked
	report.StartSeq = walk.FirstSeq
	report.EndSeq = walk.LastSeq
	report.FirstBreak = walk.Break
	switch {
	case walk.Break != nil:
		report.Status = "broken"
	case walk.Checked == 0:
		report.Status = "empty"
	default:
		report.Status = "ok"
	}

	if len(checkpoints) > 0 {
		keys, keyErr := fetchKeys(client)
		if keyErr != nil {
			fmt.Fprintf(cmd.ErrOrStderr(), "note: public keys are unavailable (%v)\n", keyErr)
		}
		report.Checkpoints = checkCheckpoints(checkpoints, keys, walk)
		for _, checked := range report.Checkpoints {
			if checkpointFails(checked) {
				report.Status = "broken"
				if report.FirstBreak == nil {
					report.FirstBreak = &verify.Break{
						Kind:   "checkpoint_mismatch",
						Seq:    checked.Seq,
						Detail: checked.Detail,
					}
				}
			}
		}
	}

	if report.ServerStatus != "" {
		serverBroken := report.ServerStatus == "broken"
		localBroken := report.Status == "broken"
		report.ServerAgrees = serverBroken == localBroken
	}
	report.Proves = map[string]interface{}{
		"order_and_no_deletion_between": []int64{report.StartSeq, report.EndSeq},
		"rows_not_verified": map[string]int64{
			"below_purge_floor": status.PrunedBelowSeq,
			"not_yet_sealed":    status.UnsealedRows,
		},
		"note": "a clean walk shows the rows were not reordered, removed or " +
			"edited after sealing. It does not show they were true when written.",
	}

	if auditJSON {
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(report); err != nil {
			return err
		}
	} else {
		printAuditVerify(cmd, status, report)
	}
	if report.Status == "broken" {
		return fmt.Errorf("the audit chain is broken at sequence %d", breakSeq(report.FirstBreak))
	}
	return nil
}

func printAuditVerify(cmd *cobra.Command, status chainStatus, report auditVerifyReport) {
	out := cmd.OutOrStdout()
	switch report.Status {
	case "ok":
		fmt.Fprintf(out, "Chain intact: %d rows, sequences %d to %d.\n",
			report.CheckedRows, report.StartSeq, report.EndSeq)
	case "empty":
		fmt.Fprintln(out, "Nothing to verify: no sealed rows in this range.")
	default:
		fmt.Fprintf(out, "CHAIN BROKEN after %d good rows.\n", report.CheckedRows)
		if report.FirstBreak != nil {
			fmt.Fprintf(out, "  first break: %s at sequence %d\n",
				report.FirstBreak.Kind, report.FirstBreak.Seq)
			if report.FirstBreak.RowID != "" {
				fmt.Fprintf(out, "  row:         %s\n", report.FirstBreak.RowID)
			}
			fmt.Fprintf(out, "  detail:      %s\n", report.FirstBreak.Detail)
			if report.FirstBreak.Expected != "" {
				fmt.Fprintf(out, "  expected:    %s\n", report.FirstBreak.Expected)
				fmt.Fprintf(out, "  computed:    %s\n", report.FirstBreak.Found)
			}
		}
	}
	for _, checked := range report.Checkpoints {
		state := "verified"
		if checkpointFails(checked) {
			state = "FAILED"
		}
		fmt.Fprintf(out, "Checkpoint at sequence %d: %s (key %s)\n", checked.Seq, state, checked.KeyID)
		if checked.Detail != "" {
			fmt.Fprintf(out, "  %s\n", checked.Detail)
		}
	}
	if report.ServerStatus != "" && !report.ServerAgrees {
		fmt.Fprintf(out, "\nThe server reports %q and this local walk reports %q. Trust the walk: it used the row content.\n",
			report.ServerStatus, report.Status)
	}
	if status.UnsealedRows > 0 {
		fmt.Fprintf(out, "\n%d row(s) are written but not sealed yet, so they are outside this result.\n", status.UnsealedRows)
	}
	if status.PrunedBelowSeq > 0 {
		fmt.Fprintf(out, "Rows below sequence %d were removed by the retention purge under policy, not verified here.\n", status.PrunedBelowSeq)
	}
	fmt.Fprintln(out, "\nThis shows the rows were not reordered, removed or edited after sealing.")
	fmt.Fprintln(out, "It does not show they were true when they were written.")
}

func runAuditKeys(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	keys, err := fetchKeys(client)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if auditJSON {
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		return encoder.Encode(keys)
	}
	fmt.Fprintf(out, "Signature format: %s\n", keys.SignedBytesFormat)
	fmt.Fprintf(out, "Active key:       %s\n\n", keys.ActiveKeyID)
	for _, key := range keys.Keys {
		state := "retired " + key.RetiredAt
		if key.Active {
			state = "active"
		}
		fmt.Fprintf(out, "%s  %s  %s\n", key.KeyID, key.Algorithm, state)
		fmt.Fprintf(out, "  %s\n", key.PublicKey)
	}
	fmt.Fprintln(out, "\nKeep your own copy. A key fetched at verification time only shows")
	fmt.Fprintln(out, "the bundle matches whatever key the server serves you today.")
	return nil
}

// checkpointOutsideWalk is reported when a checkpoint's sequence was never
// in the rows this walk fetched. A valid signature still stands; it just
// was not held against local rows, so matches_local_rows stays false.
const checkpointOutsideWalk = "outside the walked range"

// checkpointFails reports whether this checkpoint contradicts the walk.
// A signature that verifies for a sequence we did not walk is not a break.
func checkpointFails(v checkpointVerdict) bool {
	if !v.SignatureOK {
		return true
	}
	if v.Detail == checkpointOutsideWalk {
		return false
	}
	return !v.MatchesLocalRow
}

// checkCheckpoints verifies each anchor's signature and holds it against the
// rows this walk actually fetched.
func checkCheckpoints(checkpoints []chainCheckpoint, keys verify.KeyList, walk *verify.ChainWalk) []checkpointVerdict {
	verdicts := make([]checkpointVerdict, 0, len(checkpoints))
	for _, checkpoint := range checkpoints {
		verdict := checkpointVerdict{Seq: checkpoint.Seq, KeyID: checkpoint.SigningKeyID}
		if checkpoint.SignatureDocument == nil {
			verdict.Detail = "this checkpoint carries no signature, so it anchors nothing outside the database"
			verdicts = append(verdicts, verdict)
			continue
		}
		digest, err := verify.DigestOf(checkpoint.SignedPayload)
		if err != nil {
			verdict.Detail = fmt.Sprintf("checkpoint payload cannot be canonicalised: %v", err)
			verdicts = append(verdicts, verdict)
			continue
		}
		key, found := keys.Find(checkpoint.SigningKeyID)
		if !found {
			verdict.Detail = "no published public key for " + checkpoint.SigningKeyID
			verdicts = append(verdicts, verdict)
			continue
		}
		if err := verify.CheckSignature(*checkpoint.SignatureDocument, key, digest); err != nil {
			verdict.Detail = err.Error()
			verdicts = append(verdicts, verdict)
			continue
		}
		verdict.SignatureOK = true
		observed, walked := walk.Observed[checkpoint.Seq]
		if !walked {
			verdict.Detail = checkpointOutsideWalk
			verdicts = append(verdicts, verdict)
			continue
		}
		if observed != checkpoint.ChainHash {
			verdict.Detail = fmt.Sprintf(
				"the checkpoint anchors %s at this sequence but the rows served now hash to %s",
				checkpoint.ChainHash, observed)
			verdicts = append(verdicts, verdict)
			continue
		}
		verdict.MatchesLocalRow = true
		verdicts = append(verdicts, verdict)
	}
	sort.Slice(verdicts, func(i, j int) bool { return verdicts[i].Seq < verdicts[j].Seq })
	return verdicts
}

func fetchSegment(client *api.Client, afterSeq int64) (verify.Segment, error) {
	var segment verify.Segment
	path := fmt.Sprintf("%s?after_seq=%d&limit=%d", auditChainSegmentPath, afterSeq, auditSegmentPageSize)
	err := client.Get(path, &segment)
	return segment, err
}

func fetchCheckpoints(client *api.Client, afterSeq int64) ([]chainCheckpoint, error) {
	after := afterSeq - 1
	if after < 0 {
		after = 0
	}
	var checkpoints []chainCheckpoint
	path := fmt.Sprintf("%s?after_seq=%d&limit=%d", auditChainCheckpointsPath, after, 200)
	err := client.Get(path, &checkpoints)
	return checkpoints, err
}

func fetchKeys(client *api.Client) (verify.KeyList, error) {
	var keys verify.KeyList
	err := client.Get(signingKeysPath, &keys)
	return keys, err
}

// trimToEnd cuts a page at --end-seq, so a bounded range does not silently
// verify past what the caller asked about.
func trimToEnd(entries []verify.SegmentEntry, endSeq int64) []verify.SegmentEntry {
	if endSeq <= 0 {
		return entries
	}
	for index, entry := range entries {
		if entry.Seq > endSeq {
			return entries[:index]
		}
	}
	return entries
}

func seqQuery(startSeq, endSeq int64) string {
	query := ""
	if startSeq > 0 {
		query = fmt.Sprintf("start_seq=%d", startSeq)
	}
	if endSeq > 0 {
		if query != "" {
			query += "&"
		}
		query += fmt.Sprintf("end_seq=%d", endSeq)
	}
	return query
}

func breakSeq(found *verify.Break) int64 {
	if found == nil {
		return 0
	}
	return found.Seq
}
