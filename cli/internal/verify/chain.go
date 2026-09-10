package verify

import (
	"crypto/sha256"
	"encoding/hex"
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
)

// SegmentEntry is one row's chain material as the segment endpoint serves it.
type SegmentEntry struct {
	Seq      int64                  `json:"seq"`
	RowID    string                 `json:"row_id"`
	PrevHash string                 `json:"prev_hash"`
	RowHash  string                 `json:"row_hash"`
	Payload  map[string]interface{} `json:"payload"`
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
		if computed != entry.RowHash {
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
		if w.watch[entry.Seq] {
			w.Observed[entry.Seq] = computed
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
