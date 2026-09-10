package verify

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
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

// maxArchiveBytes bounds what a verifier will expand from an archive it was
// handed. A period export is a compliance artifact of known scale, and a
// verifier that can be made to allocate without limit by the file it is
// checking is not much of a verifier.
const maxArchiveBytes = 512 << 20

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

// ReadExport unpacks a period export and checks it against its own manifest.
func ReadExport(archive []byte) (ExportResult, error) {
	result := ExportResult{ArchiveSha256: DigestOfBytes(archive)}
	members, err := untar(archive)
	if err != nil {
		return result, err
	}
	manifestBody, ok := members[ManifestMember]
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
		computed := DigestOfBytes(body)
		member := MemberResult{
			Name:     name,
			Declared: wanted,
			Computed: computed,
			Size:     len(body),
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

	if body, present := members[SignatureMember]; present {
		var document SignatureDocument
		if err := json.Unmarshal(body, &document); err != nil {
			return result, fmt.Errorf("%s is not JSON: %w", SignatureMember, err)
		}
		result.Signature = &document
	}
	return result, nil
}

// untar expands a gzipped tar into memory, refusing paths that try to escape.
func untar(archive []byte) (map[string][]byte, error) {
	gz, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		return nil, fmt.Errorf("not a gzip archive: %w", err)
	}
	defer func() { _ = gz.Close() }()
	reader := tar.NewReader(gz)
	members := map[string][]byte{}
	budget := int64(maxArchiveBytes)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("not a tar archive: %w", err)
		}
		if header.Typeflag != tar.TypeReg {
			continue
		}
		body, err := io.ReadAll(io.LimitReader(reader, budget+1))
		if err != nil {
			return nil, fmt.Errorf("cannot read member %q: %w", header.Name, err)
		}
		budget -= int64(len(body))
		if budget < 0 {
			return nil, errors.New("archive expands past the size a verifier will hold in memory")
		}
		members[header.Name] = body
	}
	return members, nil
}
