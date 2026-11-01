package verify

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
)

// GenesisHash is the prev_hash of the first row in an account's chain.
const GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000"

// RowDomainV1 is the domain separator hashed in front of every row payload.
// It is served with each segment as well, so a future version does not need a
// new CLI, but a default here means a walk never depends on the server to
// tell it how to hash.
const RowDomainV1 = "preloop.audit.chain/v1\n"

// Break kinds, spelled the same as the server's so a report from either side
// can be compared without translation.
const (
	BreakMissingRow   = "missing_row"
	BreakPrevHash     = "prev_hash_mismatch"
	BreakRowHash      = "row_hash_mismatch"
	BreakDuplicateSeq = "duplicate_seq"
	// BreakRedactionUnanchored is a row rewritten after sealing whose
	// redaction_applied event does not list it with the hash it has now.
	BreakRedactionUnanchored = "redaction_unanchored"
)

// ActionRedactionApplied is the audit action of the row that vouches for
// rows rewritten by a stored-row redaction.
const ActionRedactionApplied = "redaction_applied"

// SegmentEntry is one row's chain material as the segment endpoint serves it.
type SegmentEntry struct {
	Seq      int64  `json:"seq"`
	RowID    string `json:"row_id"`
	PrevHash string `json:"prev_hash"`
	RowHash  string `json:"row_hash"`
	// RedactedRowHash is set when a redaction rewrote the row after it was
	// sealed: the hash of the payload as served. RowHash stays the value the
	// next row links to.
	RedactedRowHash  string                 `json:"redacted_row_hash,omitempty"`
	RedactionEventID string                 `json:"redaction_event_id,omitempty"`
	Payload          map[string]interface{} `json:"payload"`
}

// Segment is one page of the chain.
type Segment struct {
	AccountID      string         `json:"account_id"`
	RowDomain      string         `json:"row_domain"`
	AfterSeq       int64          `json:"after_seq"`
	HeadSeq        int64          `json:"head_seq"`
	PrunedBelowSeq int64          `json:"pruned_below_seq"`
	GenesisHash    string         `json:"genesis_hash"`
	Entries        []SegmentEntry `json:"entries"`
	HasMore        bool           `json:"has_more"`
	Note           string         `json:"note"`
}

// UnmarshalJSON decodes a segment with json.Number so a number keeps the
// spelling the server emitted. encoding/json would turn 0.0 into float64 and
// reprint it as 0, which no longer matches the sealed row hash.
func (s *Segment) UnmarshalJSON(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	type segmentAlias Segment
	var alias segmentAlias
	if err := decoder.Decode(&alias); err != nil {
		return err
	}
	*s = Segment(alias)
	return nil
}

// Break is the first place a local walk stopped agreeing with the chain.
type Break struct {
	Kind     string `json:"kind"`
	Seq      int64  `json:"seq"`
	RowID    string `json:"row_id,omitempty"`
	Expected string `json:"expected,omitempty"`
	Found    string `json:"found,omitempty"`
	Detail   string `json:"detail"`
}

// ChainWalk accumulates a verification across pages of the chain.
type ChainWalk struct {
	rowDomain    string
	expectedPrev string
	expectedSeq  int64
	started      bool

	// Checked is how many rows this walk recomputed.
	Checked int64
	// FirstSeq and LastSeq bound what was actually covered, which is the
	// only range the result speaks about.
	FirstSeq int64
	LastSeq  int64
	// Break is nil while the chain still agrees with itself.
	Break *Break

	watch map[int64]bool
	// Observed holds the recomputed row hash at each watched sequence.
	Observed map[int64]string

	// Redacted counts rows accepted against their redacted hash.
	Redacted int64
	// pendingRedactions maps a rewritten row to its event id and hash until
	// the event's own row is fed.
	pendingRedactions map[string]pendingRedaction
	// redactionEvents maps an event row id to the rows and hashes it lists.
	redactionEvents map[string]map[string]string
}

type pendingRedaction struct {
	seq     int64
	eventID string
	hash    string
}

// NewChainWalk starts a walk that expects the row after afterSeq next.
//
// expectedPrev is the row_hash the first row must commit to: the genesis hash
// when starting from the beginning, otherwise the hash of the row before the
// range. Passing an empty string means "take the first row's word for it",
// which is the honest state when a walk starts in the middle and the caller
// has nothing to anchor against.
func NewChainWalk(rowDomain string, afterSeq int64, expectedPrev string) *ChainWalk {
	return &ChainWalk{
		rowDomain:    rowDomain,
		expectedPrev: expectedPrev,
		expectedSeq:  afterSeq + 1,
	}
}

// RowHash recomputes one row's hash from the payload the API served.
func RowHash(rowDomain string, payload interface{}) (string, error) {
	body, err := CanonicalJSON(payload)
	if err != nil {
		return "", err
	}
	digest := sha256.New()
	digest.Write([]byte(rowDomain))
	digest.Write(body)
	return hex.EncodeToString(digest.Sum(nil)), nil
}

// Feed walks one page. It returns false once a break is found: everything
// after the first break is a consequence of it, and reporting the cascade
// would bury the one row that matters.
func (w *ChainWalk) Feed(entries []SegmentEntry) bool {
	for _, entry := range entries {
		if w.Break != nil {
			return false
		}
		if entry.Seq < w.expectedSeq {
			w.Break = &Break{
				Kind:   BreakDuplicateSeq,
				Seq:    entry.Seq,
				RowID:  entry.RowID,
				Detail: "two rows share one chain sequence",
			}
			return false
		}
		if entry.Seq > w.expectedSeq {
			w.Break = &Break{
				Kind: BreakMissingRow,
				Seq:  w.expectedSeq,
				Detail: fmt.Sprintf(
					"sequence %d is missing; the chain jumps to %d",
					w.expectedSeq, entry.Seq,
				),
			}
			return false
		}
		if w.expectedPrev != "" && entry.PrevHash != w.expectedPrev {
			w.Break = &Break{
				Kind:     BreakPrevHash,
				Seq:      entry.Seq,
				RowID:    entry.RowID,
				Expected: w.expectedPrev,
				Found:    entry.PrevHash,
				Detail:   "this row does not point at the row before it",
			}
			return false
		}
		computed, err := RowHash(w.rowDomain, entry.Payload)
		if err != nil {
			w.Break = &Break{
				Kind:   BreakRowHash,
				Seq:    entry.Seq,
				RowID:  entry.RowID,
				Detail: fmt.Sprintf("row payload cannot be canonicalised: %v", err),
			}
			return false
		}
		redacted := false
		if computed != entry.RowHash && entry.RedactedRowHash != "" &&
			computed == entry.RedactedRowHash && entry.RedactionEventID != "" {
			// Accepted for now; Finish checks it against the event row.
			redacted = true
			if w.pendingRedactions == nil {
				w.pendingRedactions = map[string]pendingRedaction{}
			}
			w.pendingRedactions[entry.RowID] = pendingRedaction{
				seq: entry.Seq, eventID: entry.RedactionEventID, hash: computed,
			}
			w.Redacted++
		}
		if computed != entry.RowHash && !redacted {
			w.Break = &Break{
				Kind:     BreakRowHash,
				Seq:      entry.Seq,
				RowID:    entry.RowID,
				Expected: entry.RowHash,
				Found:    computed,
				Detail:   "the row content does not match the hash sealed for it",
			}
			return false
		}
		if !w.started {
			w.FirstSeq = entry.Seq
			w.started = true
		}
		w.LastSeq = entry.Seq
		w.Checked++
		w.noteRedactionEvent(entry)
		if w.watch[entry.Seq] {
			// The chain links to RowHash; for a rewritten row that is not
			// the hash of today's payload, and the checkpoint anchored it.
			w.Observed[entry.Seq] = entry.RowHash
		}
		w.expectedPrev = entry.RowHash
		w.expectedSeq = entry.Seq + 1
	}
	return true
}

// Head is the row_hash the walk ended on, which is what a checkpoint at that
// sequence has to agree with.
func (w *ChainWalk) Head() string {
	return w.expectedPrev
}

// OK reports whether the walk found no break. An empty range is not a
// failure: there was nothing to disagree with.
func (w *ChainWalk) OK() bool {
	return w.Break == nil
}

// Watch asks the walk to remember the row hash at these sequences, so a
// checkpoint taken at one of them can be held against what the rows in hand
// actually hash to today.
func (w *ChainWalk) Watch(seqs []int64) {
	if w.watch == nil {
		w.watch = map[int64]bool{}
	}
	if w.Observed == nil {
		w.Observed = map[int64]string{}
	}
	for _, seq := range seqs {
		w.watch[seq] = true
	}
}

// SetRowDomain adopts the domain separator the server serves with a segment.
// A server that changes it mid walk changes every hash from that point, which
// the walk then reports as a break rather than quietly accepting.
func (w *ChainWalk) SetRowDomain(domain string) {
	if domain != "" {
		w.rowDomain = domain
	}
}

// noteRedactionEvent records the rows a redaction_applied entry lists.
func (w *ChainWalk) noteRedactionEvent(entry SegmentEntry) {
	if entry.Payload["action"] != ActionRedactionApplied {
		return
	}
	details, _ := entry.Payload["details"].(map[string]interface{})
	rows, _ := details["rows"].([]interface{})
	listing := map[string]string{}
	for _, item := range rows {
		row, ok := item.(map[string]interface{})
		if !ok {
			continue
		}
		id, _ := row["id"].(string)
		hash, _ := row["redacted_row_hash"].(string)
		if id != "" && hash != "" {
			listing[id] = hash
		}
	}
	if w.redactionEvents == nil {
		w.redactionEvents = map[string]map[string]string{}
	}
	w.redactionEvents[entry.RowID] = listing
}

// Finish checks every rewritten row against the redaction event it names.
// A row whose event was walked but does not list it with its current hash is
// a break. A row whose event was not in the walked range cannot be decided
// here and is returned as unresolved; widen the range to include it.
func (w *ChainWalk) Finish() (unresolved int64) {
	var first *Break
	for rowID, pending := range w.pendingRedactions {
		listing, seen := w.redactionEvents[pending.eventID]
		if !seen {
			unresolved++
			continue
		}
		if listing[rowID] != pending.hash && (first == nil || pending.seq < first.Seq) {
			first = &Break{
				Kind:     BreakRedactionUnanchored,
				Seq:      pending.seq,
				RowID:    rowID,
				Expected: listing[rowID],
				Found:    pending.hash,
				Detail:   "the row was rewritten after sealing, but its redaction event does not list it with this hash",
			}
		}
	}
	if first != nil && (w.Break == nil || first.Seq < w.Break.Seq) {
		w.Break = first
	}
	return unresolved
}
