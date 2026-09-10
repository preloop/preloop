package verify

import (
	"fmt"
	"testing"
)

// buildChain makes a chain of n rows the way the sealer does, so the walk is
// tested against material that hashes the same way the platform's does.
func buildChain(t *testing.T, count int) []SegmentEntry {
	t.Helper()
	entries := make([]SegmentEntry, 0, count)
	prev := GenesisHash
	for index := 1; index <= count; index++ {
		payload := map[string]interface{}{
			"seq":           index,
			"prev_hash":     prev,
			"action":        fmt.Sprintf("action_%d", index),
			"resource_type": "tool",
			"status":        "success",
		}
		hash, err := RowHash(RowDomainV1, payload)
		if err != nil {
			t.Fatal(err)
		}
		entries = append(entries, SegmentEntry{
			Seq:      int64(index),
			RowID:    fmt.Sprintf("row-%d", index),
			PrevHash: prev,
			RowHash:  hash,
			Payload:  payload,
		})
		prev = hash
	}
	return entries
}

func walkAll(entries []SegmentEntry) *ChainWalk {
	walk := NewChainWalk(RowDomainV1, 0, GenesisHash)
	walk.Feed(entries)
	return walk
}

func TestAWholeChainWalksClean(t *testing.T) {
	walk := walkAll(buildChain(t, 25))

	if !walk.OK() {
		t.Fatalf("a good chain reported a break: %+v", walk.Break)
	}
	if walk.Checked != 25 || walk.FirstSeq != 1 || walk.LastSeq != 25 {
		t.Fatalf("checked %d rows, %d to %d", walk.Checked, walk.FirstSeq, walk.LastSeq)
	}
}

func TestAnEditedRowIsCaughtAtItsOwnSequence(t *testing.T) {
	entries := buildChain(t, 10)
	// The edit a tamperer would make: change the record, leave the hashes.
	entries[4].Payload["status"] = "failure"

	walk := walkAll(entries)

	if walk.OK() {
		t.Fatal("an edited row walked clean")
	}
	if walk.Break.Kind != BreakRowHash || walk.Break.Seq != 5 {
		t.Fatalf("break = %+v", walk.Break)
	}
	if walk.Break.RowID != "row-5" {
		t.Fatalf("break names row %q", walk.Break.RowID)
	}
	if walk.Checked != 4 {
		t.Fatalf("checked %d rows before the break", walk.Checked)
	}
}

func TestARowDeletedFromTheMiddleIsCaught(t *testing.T) {
	entries := buildChain(t, 10)
	entries = append(entries[:3], entries[4:]...)

	walk := walkAll(entries)

	if walk.OK() {
		t.Fatal("a deleted row walked clean")
	}
	if walk.Break.Kind != BreakMissingRow || walk.Break.Seq != 4 {
		t.Fatalf("break = %+v", walk.Break)
	}
}

func TestARelinkedRowIsCaughtByItsPrevHash(t *testing.T) {
	entries := buildChain(t, 6)
	// Re-hash a row over a different predecessor: the row hash is now
	// self-consistent, and the link to the row before it is not.
	entries[3].PrevHash = GenesisHash
	entries[3].Payload["prev_hash"] = GenesisHash
	rehashed, err := RowHash(RowDomainV1, entries[3].Payload)
	if err != nil {
		t.Fatal(err)
	}
	entries[3].RowHash = rehashed

	walk := walkAll(entries)

	if walk.OK() {
		t.Fatal("a relinked row walked clean")
	}
	if walk.Break.Kind != BreakPrevHash || walk.Break.Seq != 4 {
		t.Fatalf("break = %+v", walk.Break)
	}
}

func TestOnlyTheFirstBreakIsReported(t *testing.T) {
	entries := buildChain(t, 10)
	entries[2].Payload["status"] = "failure"
	entries[7].Payload["status"] = "failure"

	walk := walkAll(entries)

	// Everything after the first break is a consequence of it, and burying
	// the one row that matters under the cascade helps nobody.
	if walk.Break.Seq != 3 {
		t.Fatalf("break = %+v", walk.Break)
	}
}

func TestAWalkResumesAcrossPages(t *testing.T) {
	entries := buildChain(t, 30)
	walk := NewChainWalk(RowDomainV1, 0, GenesisHash)

	walk.Feed(entries[:10])
	walk.Feed(entries[10:20])
	walk.Feed(entries[20:])

	if !walk.OK() || walk.Checked != 30 {
		t.Fatalf("paged walk: checked %d, break %+v", walk.Checked, walk.Break)
	}
	if walk.Head() != entries[29].RowHash {
		t.Fatal("the walk did not end on the last row hash")
	}
}

func TestAWalkStartingInTheMiddleTakesTheFirstRowsWord(t *testing.T) {
	entries := buildChain(t, 20)

	// Starting after a retention purge there is nothing to anchor against,
	// and pretending otherwise would report the purge as tampering.
	walk := NewChainWalk(RowDomainV1, 10, "")
	walk.Feed(entries[10:])

	if !walk.OK() || walk.FirstSeq != 11 || walk.Checked != 10 {
		t.Fatalf("partial walk: %d rows from %d, break %+v", walk.Checked, walk.FirstSeq, walk.Break)
	}
}

func TestAnEmptyRangeIsNotAFailure(t *testing.T) {
	walk := NewChainWalk(RowDomainV1, 0, GenesisHash)

	walk.Feed(nil)

	if !walk.OK() || walk.Checked != 0 {
		t.Fatalf("empty walk: %+v", walk.Break)
	}
}

func TestWatchedSequencesRecordWhatTheRowsHashToNow(t *testing.T) {
	entries := buildChain(t, 12)
	walk := NewChainWalk(RowDomainV1, 0, GenesisHash)
	walk.Watch([]int64{10})

	walk.Feed(entries)

	// This is what makes a checkpoint worth keeping: the anchor from before
	// is compared with what the rows served today hash to.
	if walk.Observed[10] != entries[9].RowHash {
		t.Fatalf("observed %q at seq 10", walk.Observed[10])
	}
}

func TestARepeatedSequenceIsCaught(t *testing.T) {
	entries := buildChain(t, 5)
	entries = append(entries, entries[4])

	walk := walkAll(entries)

	if walk.OK() || walk.Break.Kind != BreakDuplicateSeq {
		t.Fatalf("break = %+v", walk.Break)
	}
}
