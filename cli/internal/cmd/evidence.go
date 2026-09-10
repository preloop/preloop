// Signed export and evidence pack verification (#558).
//
// Everything here works on bytes the caller already holds. That is the only
// verification worth running: a check that downloads the artifact and the
// answer from the same place at the same moment proves very little.
//
// Two shapes are handled, because two things are signed. A period export
// (from `preloop` retention exports) carries manifest.json and a detached
// signature.json over the manifest bytes. An evidence pack is content
// addressed the moment it is stored, so appending a signature to the archive
// would change the digest the receipt already promised; its signature is
// served beside the pack on the receipt instead.

package cmd

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/verify"
)

const evidenceStatusPathFormat = "/api/v1/flows/executions/%s/evidence-status"

var (
	evidenceExecutionID string
	evidencePublicKey   string
	evidenceOffline     bool
	evidenceJSON        bool
)

// evidenceReceipt is the part of an evidence status response this command
// needs: the detached signature, with the payload that was signed.
type evidenceReceipt struct {
	Status       string                    `json:"status"`
	ArtifactID   string                    `json:"artifact_id"`
	Sha256       string                    `json:"sha256"`
	SizeBytes    int64                     `json:"size_bytes"`
	SigningKeyID string                    `json:"signing_key_id"`
	Signature    *verify.SignatureDocument `json:"signature"`
}

// evidenceVerifyReport is the --json shape.
type evidenceVerifyReport struct {
	Kind          string                 `json:"kind"`
	File          string                 `json:"file"`
	Sha256        string                 `json:"sha256"`
	ContentOK     bool                   `json:"content_ok"`
	SignatureOK   bool                   `json:"signature_ok"`
	KeyID         string                 `json:"key_id,omitempty"`
	KeySource     string                 `json:"key_source,omitempty"`
	SignedAt      string                 `json:"signed_at,omitempty"`
	Members       []verify.MemberResult  `json:"members,omitempty"`
	MembersDigest map[string]interface{} `json:"members_digest,omitempty"`
	Problems      []string               `json:"problems,omitempty"`
	Proves        string                 `json:"proves"`
}

// evidenceCmd is the parent for evidence verification.
var evidenceCmd = &cobra.Command{
	Use:   "evidence",
	Short: "Verify signed exports and evidence packs",
	Long:  `Check a bundle you were given against the signature Preloop made for it.`,
}

// evidenceVerifyCmd implements `preloop evidence verify`.
var evidenceVerifyCmd = &cobra.Command{
	Use:   "verify <archive>",
	Short: "Verify a period export or an evidence pack against its signature",
	Long: `Verify bytes you hold, not bytes fetched for the occasion.

Period export (default):
  Expands the archive, recomputes the sha256 of every member and the digest
  over the member list, then checks the detached Ed25519 signature in
  signature.json against the account's public key.

Evidence pack (--execution):
  Hashes the archive and compares it with the digest inside the signed
  payload the receipt carries, then checks the signature over that payload.
  The pack is signed at capture, not at download, so re-serving it cannot
  change what was signed.

Use --public-key with a base64 key or a file holding one to verify without
talking to us at all. Otherwise the public key is fetched from the account,
which is weaker: it shows the bundle matches the key the server serves today.

What a pass means: this bundle is the one Preloop built and nothing in it has
moved since. What it does not mean: that the records inside were true when
they were written. The signing key lives on the same platform that wrote them.

Exit status is 1 when verification fails.

Examples:
  preloop evidence verify preloop-period-export-2026-04-01-to-2026-05-01.tar.gz
  preloop evidence verify evidence.tar.gz --execution 8f1c...
  preloop evidence verify bundle.tar.gz --public-key ./account-key.pub`,
	Args: cobra.ExactArgs(1),
	RunE: runEvidenceVerify,
}

func init() {
	evidenceVerifyCmd.Flags().StringVar(&evidenceExecutionID, "execution", "", "verify an evidence pack captured for this execution")
	evidenceVerifyCmd.Flags().StringVar(&evidencePublicKey, "public-key", "", "base64 Ed25519 public key, or a path to a file holding one")
	evidenceVerifyCmd.Flags().BoolVar(&evidenceOffline, "offline", false, "check digests only, never contact the API")
	evidenceVerifyCmd.Flags().BoolVar(&evidenceJSON, "json", false, "emit the verdict as JSON")

	evidenceCmd.AddCommand(evidenceVerifyCmd)
}

func runEvidenceVerify(cmd *cobra.Command, args []string) error {
	archive, err := os.ReadFile(args[0])
	if err != nil {
		return fmt.Errorf("could not read %s: %w", args[0], err)
	}
	report := evidenceVerifyReport{File: args[0], Sha256: verify.DigestOfBytes(archive)}
	if evidenceExecutionID != "" {
		report.Kind = "evidence_pack"
		err = verifyEvidencePack(cmd, archive, &report)
	} else {
		report.Kind = "period_export"
		err = verifyPeriodExport(cmd, archive, &report)
	}
	if err != nil {
		return err
	}
	if evidenceJSON {
		encoder := json.NewEncoder(cmd.OutOrStdout())
		encoder.SetIndent("", "  ")
		if encodeErr := encoder.Encode(report); encodeErr != nil {
			return encodeErr
		}
	} else {
		printEvidenceVerify(cmd, report)
	}
	if !report.ContentOK || !report.SignatureOK {
		return fmt.Errorf("verification failed for %s", args[0])
	}
	return nil
}

func verifyPeriodExport(cmd *cobra.Command, archive []byte, report *evidenceVerifyReport) error {
	result, err := verify.ReadExport(archive)
	if err != nil {
		return err
	}
	report.ContentOK = result.ContentOK()
	report.Members = result.Members
	report.MembersDigest = map[string]interface{}{
		"declared": result.MembersDigest.Declared,
		"computed": result.MembersDigest.Computed,
		"ok":       result.MembersDigest.OK,
	}
	report.Problems = result.Problems
	report.Proves = "the archive is the one Preloop built and nothing in it has moved since. " +
		"Not that the records were true when written."

	if result.Signature == nil {
		report.Problems = append(report.Problems, "this archive carries no signature")
		return nil
	}
	report.KeyID = result.Signature.KeyID
	report.SignedAt = result.Signature.SignedAt
	key, source, err := resolvePublicKey(cmd, result.Signature.KeyID, "")
	if err != nil {
		report.Problems = append(report.Problems, err.Error())
		return nil
	}
	report.KeySource = source
	if err := verify.CheckSignature(*result.Signature, key, result.ManifestSha256); err != nil {
		report.Problems = append(report.Problems, err.Error())
		return nil
	}
	report.SignatureOK = true
	return nil
}

func verifyEvidencePack(cmd *cobra.Command, archive []byte, report *evidenceVerifyReport) error {
	if evidenceOffline {
		return fmt.Errorf("an evidence pack signature is served with the receipt, so --offline cannot verify one")
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	var receipt evidenceReceipt
	path := fmt.Sprintf(evidenceStatusPathFormat, evidenceExecutionID)
	if err := client.Get(path, &receipt); err != nil {
		return fmt.Errorf("could not read the evidence receipt: %w", err)
	}
	report.Proves = "this archive is the evidence pack Preloop captured for that execution. " +
		"Not that its contents were true when captured."
	if receipt.Signature == nil {
		report.Problems = append(report.Problems,
			"this pack has no signature: it was captured before signing existed, or the account has no usable key")
		return nil
	}
	document := *receipt.Signature
	report.KeyID = document.KeyID
	report.SignedAt = document.SignedAt

	// The digest inside the signed payload is the one claim about these bytes
	// that was made at capture time.
	signedDigest, _ := document.Payload["archive_sha256"].(string)
	if signedDigest == "" {
		report.Problems = append(report.Problems, "the signed payload names no archive digest")
		return nil
	}
	if signedDigest != report.Sha256 {
		report.Problems = append(report.Problems, fmt.Sprintf(
			"the signature covers archive %s but this file is %s", signedDigest, report.Sha256))
		return nil
	}
	report.ContentOK = true

	payloadDigest, err := verify.DigestOf(document.Payload)
	if err != nil {
		return fmt.Errorf("the signed payload cannot be canonicalised: %w", err)
	}
	key, source, err := resolvePublicKey(cmd, document.KeyID, receipt.SigningKeyID)
	if err != nil {
		report.Problems = append(report.Problems, err.Error())
		return nil
	}
	report.KeySource = source
	if err := verify.CheckSignature(document, key, payloadDigest); err != nil {
		report.Problems = append(report.Problems, err.Error())
		return nil
	}
	report.SignatureOK = true
	return nil
}

// resolvePublicKey prefers a key the caller brought. Fetching the key from
// the same server that made the signature is the weaker check, and the report
// says which one happened.
func resolvePublicKey(cmd *cobra.Command, keyID, fallbackKeyID string) (verify.PublicKey, string, error) {
	if keyID == "" {
		keyID = fallbackKeyID
	}
	if evidencePublicKey != "" {
		raw := strings.TrimSpace(evidencePublicKey)
		if body, err := os.ReadFile(raw); err == nil {
			raw = strings.TrimSpace(string(body))
		}
		if _, err := base64.StdEncoding.DecodeString(raw); err != nil {
			return verify.PublicKey{}, "", fmt.Errorf("--public-key is neither base64 nor a readable file")
		}
		return verify.PublicKey{
			KeyID:     keyID,
			Algorithm: verify.AlgorithmEd25519,
			PublicKey: raw,
		}, "flag", nil
	}
	if evidenceOffline {
		return verify.PublicKey{}, "", fmt.Errorf("--offline was given and no --public-key: digests checked, signature not")
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return verify.PublicKey{}, "", err
	}
	keys, err := fetchKeys(client)
	if err != nil {
		return verify.PublicKey{}, "", fmt.Errorf("could not fetch the public keys: %w", err)
	}
	key, found := keys.Find(keyID)
	if !found {
		return verify.PublicKey{}, "", fmt.Errorf("the account publishes no key with id %s", keyID)
	}
	return key, "api", nil
}

func printEvidenceVerify(cmd *cobra.Command, report evidenceVerifyReport) {
	out := cmd.OutOrStdout()
	fmt.Fprintf(out, "File:   %s\n", report.File)
	fmt.Fprintf(out, "sha256: %s\n", report.Sha256)
	for _, member := range report.Members {
		state := "ok"
		if !member.OK {
			state = "ALTERED"
		}
		fmt.Fprintf(out, "  %-40s %8d bytes  %s\n", member.Name, member.Size, state)
	}
	if report.MembersDigest != nil {
		state := "ok"
		if report.MembersDigest["ok"] != true {
			state = "MISMATCH"
		}
		fmt.Fprintf(out, "  member list digest: %s\n", state)
	}
	if report.SignatureOK {
		fmt.Fprintf(out, "\nSignature verified with key %s", report.KeyID)
		if report.KeySource == "api" {
			fmt.Fprint(out, " (fetched from the API, so it shows the bundle matches today's published key)")
		}
		fmt.Fprintln(out)
		if report.SignedAt != "" {
			fmt.Fprintf(out, "Signed at %s\n", report.SignedAt)
		}
	} else {
		fmt.Fprintln(out, "\nSIGNATURE NOT VERIFIED")
	}
	for _, problem := range report.Problems {
		fmt.Fprintf(out, "  - %s\n", problem)
	}
	fmt.Fprintf(out, "\nWhat this shows: %s\n", report.Proves)
}
