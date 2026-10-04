package verify

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
)

// Member names fixed by the server side builder
// (backend/preloop/services/retention_export.py).
const (
	ManifestMember  = "manifest.json"
	SignatureMember = "signature.json"
)

// maxStreamedBytes bounds what ReadExportFrom will read through from an
// archive. Members are digested as they stream past and never held, so the
// bound is about time, not memory. It sits above the server's default
// artifact cap (RETENTION_EXPORT_MAX_ARTIFACT_BYTES, 2 GiB, #1088) with
// room for the record members.
var maxStreamedBytes int64 = 4 << 30

// maxHeldMemberBytes bounds the members ReadExportFrom keeps in memory:
// manifest.json and signature.json, which it has to parse.
var maxHeldMemberBytes int64 = 64 << 20

// streamedMember is what a streaming read keeps of one member.
type streamedMember struct {
	digest string
	size   int
}

// MemberResult is one member of an export and whether its bytes still match
// the digest the manifest claims.
type MemberResult struct {
	Name     string `json:"name"`
	Declared string `json:"declared_sha256"`
	Computed string `json:"computed_sha256"`
	Size     int    `json:"size_bytes"`
	OK       bool   `json:"ok"`
	Problem  string `json:"problem,omitempty"`
}

// ExportResult is everything a local check of a period export can establish
// before a key is involved.
type ExportResult struct {
	ArchiveSha256  string                 `json:"archive_sha256"`
	ManifestSha256 string                 `json:"manifest_sha256"`
	Manifest       map[string]interface{} `json:"-"`
	Members        []MemberResult         `json:"members"`
	MembersDigest  struct {
		Declared string `json:"declared"`
		Computed string `json:"computed"`
		OK       bool   `json:"ok"`
	} `json:"members_digest"`
	Signature *SignatureDocument `json:"signature,omitempty"`
	// Problems are the failures found without needing a key: a member whose
	// bytes moved, a manifest that lists a member the archive does not hold.
	Problems []string `json:"problems,omitempty"`
}

// ContentOK reports whether the archive is internally consistent. It says
// nothing about who built it: that is what the signature is for.
func (r ExportResult) ContentOK() bool {
	return len(r.Problems) == 0
}

// ReadExport checks an in-memory period export against its own manifest. It
// is ReadExportFrom over a byte slice, for callers that already hold the
// archive; the CLI streams from disk instead.
func ReadExport(archive []byte) (ExportResult, error) {
	return ReadExportFrom(bytes.NewReader(archive))
}

// ReadExportFrom is ReadExport over a stream. Member bytes are hashed as
// they pass and dropped, so an export carrying session artifacts (#1088)
// is checked without holding it in memory. ArchiveSha256 covers the bytes
// read from r.
func ReadExportFrom(r io.Reader) (ExportResult, error) {
	result := ExportResult{}
	archiveHash := sha256.New()
	members, held, err := streamTar(io.TeeReader(r, archiveHash))
	if err != nil {
		return result, err
	}
	// Drain anything after the tar end so the archive digest covers the file.
	if _, err := io.Copy(archiveHash, r); err != nil {
		return result, fmt.Errorf("cannot read archive: %w", err)
	}
	result.ArchiveSha256 = hex.EncodeToString(archiveHash.Sum(nil))
	manifestBody, ok := held[ManifestMember]
	if !ok {
		return result, fmt.Errorf("the archive has no %s", ManifestMember)
	}
	result.ManifestSha256 = DigestOfBytes(manifestBody)
	manifest, err := DecodeCanonical(manifestBody)
	if err != nil {
		return result, fmt.Errorf("%s is not JSON: %w", ManifestMember, err)
	}
	object, ok := manifest.(map[string]interface{})
	if !ok {
		return result, fmt.Errorf("%s is not a JSON object", ManifestMember)
	}
	result.Manifest = object

	declared, _ := object["members"].([]interface{})
	seen := map[string]bool{ManifestMember: true, SignatureMember: true}
	for _, raw := range declared {
		entry, ok := raw.(map[string]interface{})
		if !ok {
			result.Problems = append(result.Problems, "a member entry is not an object")
			continue
		}
		name, _ := entry["name"].(string)
		wanted, _ := entry["sha256"].(string)
		seen[name] = true
		body, present := members[name]
		if !present {
			result.Members = append(result.Members, MemberResult{
				Name: name, Declared: wanted,
				Problem: "the manifest lists this member but the archive does not hold it",
			})
			result.Problems = append(result.Problems, "missing member "+name)
			continue
		}
		computed := body.digest
		member := MemberResult{
			Name:     name,
			Declared: wanted,
			Computed: computed,
			Size:     body.size,
			OK:       computed == wanted,
		}
		if !member.OK {
			member.Problem = "the bytes in the archive do not match the digest the manifest claims"
			result.Problems = append(result.Problems, "altered member "+name)
		}
		result.Members = append(result.Members, member)
	}
	for name := range members {
		if !seen[name] {
			// An extra member is not automatically an attack, but the
			// manifest is supposed to be the complete list, so an unlisted
			// file is outside everything the signature covers.
			result.Problems = append(result.Problems, "unlisted member "+name)
		}
	}
	sort.Slice(result.Members, func(i, j int) bool {
		return result.Members[i].Name < result.Members[j].Name
	})

	result.MembersDigest.Declared, _ = object["members_digest"].(string)
	if declared != nil {
		computed, err := DigestOf(declared)
		if err != nil {
			return result, fmt.Errorf("cannot canonicalise the member list: %w", err)
		}
		result.MembersDigest.Computed = computed
	}
	result.MembersDigest.OK = result.MembersDigest.Declared != "" &&
		result.MembersDigest.Declared == result.MembersDigest.Computed
	if !result.MembersDigest.OK {
		result.Problems = append(result.Problems, "members_digest does not cover this member list")
	}

	if body, present := held[SignatureMember]; present {
		var document SignatureDocument
		if err := json.Unmarshal(body, &document); err != nil {
			return result, fmt.Errorf("%s is not JSON: %w", SignatureMember, err)
		}
		result.Signature = &document
	}
	return result, nil
}

// streamTar digests every regular member of a gzipped tar and keeps only the
// manifest and signature bodies.
func streamTar(r io.Reader) (map[string]streamedMember, map[string][]byte, error) {
	gz, err := gzip.NewReader(r)
	if err != nil {
		return nil, nil, fmt.Errorf("not a gzip archive: %w", err)
	}
	defer func() { _ = gz.Close() }()
	reader := tar.NewReader(gz)
	members := map[string]streamedMember{}
	held := map[string][]byte{}
	budget := maxStreamedBytes
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, nil, fmt.Errorf("not a tar archive: %w", err)
		}
		if header.Typeflag != tar.TypeReg {
			continue
		}
		hash := sha256.New()
		var sink io.Writer = hash
		var keep *bytes.Buffer
		if header.Name == ManifestMember || header.Name == SignatureMember {
			keep = &bytes.Buffer{}
			sink = io.MultiWriter(hash, keep)
		}
		limit := budget
		if keep != nil && limit > maxHeldMemberBytes {
			limit = maxHeldMemberBytes
		}
		n, err := io.Copy(sink, io.LimitReader(reader, limit+1))
		if err != nil {
			return nil, nil, fmt.Errorf("cannot read member %q: %w", header.Name, err)
		}
		if n > limit {
			return nil, nil, errors.New("archive expands past the size a verifier will read")
		}
		budget -= n
		members[header.Name] = streamedMember{
			digest: hex.EncodeToString(hash.Sum(nil)),
			size:   int(n),
		}
		if keep != nil {
			held[header.Name] = keep.Bytes()
		}
	}
	return members, held, nil
}
