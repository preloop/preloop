#!/bin/bash
# Fail unless the distro Perl toolchain is usable.
#
# Shared by environments/perl/Dockerfile (the opt-in Codex-compatible Perl
# image) and environments/preloop/Dockerfile (the project fixture image), so
# both run one gate. It needs no network.
#
# perlver 1.40 has no --version flag (it treats the option as unknown and
# exits non-zero), so it is proven by running it on a file. The minimum
# version is read through the Perl::MinimumVersion API and compared, never
# inferred from perlver's exit status.
#
# Usage: perl-toolchain-smoke.sh [fixtures-dir]
# The fixtures directory defaults to ./fixtures next to this script.
set -euo pipefail

fixtures="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures}"

fail() {
    echo "perl-toolchain-smoke: FAIL: $*" >&2
    exit 1
}

for tool in perl cpanm perlver perlcritic prove; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing required tool: $tool"
done
for fixture in compatible-5_10.pl postfix-dereference.pl t/pass.t failing/fail.t; do
    [ -f "$fixtures/$fixture" ] || fail "missing fixture: $fixtures/$fixture"
done

echo "user: $(id -un 2>/dev/null || true) uid=$(id -u) gid=$(id -g)"
perl -e 'printf "perl: %vd (%s)\n", $^V, $^X'
for module in Perl::MinimumVersion Perl::Critic Test::More Test::Harness; do
    perl -M"$module" -e 'my $m = shift; no strict "refs"; print "$m: ", ${"${m}::VERSION"}, "\n"' "$module" ||
        fail "module does not load: $module"
done
# cpanm writes to $HOME/.cpanm even for --version. An agent user may have no
# writable HOME, so give it a throwaway one.
cpanm_home="$(mktemp -d)"
cpanm_version="$(PERL_CPANM_HOME="$cpanm_home" cpanm --version)" || fail "cpanm does not run"
rm -rf "$cpanm_home"
echo "cpanm: ${cpanm_version%%$'\n'*}"
echo "perlcritic: $(perlcritic --version)"
echo "prove: $(prove --version)"

one_liner="$(mktemp)"
trap 'rm -f "$one_liner"' EXIT
printf '%s\n' 'print "ok\n";' >"$one_liner"
perlver "$one_liner" >/dev/null || fail "perlver does not run"

minimum_version() {
    perl -MPerl::MinimumVersion -e '
        my $pmv = Perl::MinimumVersion->new($ARGV[0]) or die "cannot parse $ARGV[0]\n";
        print $pmv->minimum_version->numify, "\n";
    ' "$1"
}
compatible="$(minimum_version "$fixtures/compatible-5_10.pl")" ||
    fail "Perl::MinimumVersion cannot read compatible-5_10.pl"
newer="$(minimum_version "$fixtures/postfix-dereference.pl")" ||
    fail "Perl::MinimumVersion cannot read postfix-dereference.pl"
echo "minimum version: compatible-5_10.pl=$compatible postfix-dereference.pl=$newer"
perl -e 'exit($ARGV[0] <= 5.010 ? 0 : 1)' "$compatible" ||
    fail "compatible-5_10.pl reported $compatible, expected at most 5.010"
perl -e 'exit($ARGV[0] > 5.010 ? 0 : 1)' "$newer" ||
    fail "postfix-dereference.pl reported $newer, expected newer than 5.010"

prove "$fixtures/t/pass.t" || fail "prove rejected the passing TAP fixture"
if prove "$fixtures/failing/fail.t" >/dev/null 2>&1; then
    fail "prove accepted the deliberately failing TAP fixture"
fi
echo "perl-toolchain-smoke: OK"
