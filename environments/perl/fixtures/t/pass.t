# Synthetic TAP fixture: must pass through prove.
use strict;
use warnings;
use Test::More tests => 2;

ok(1, 'truth holds');
is(2 + 2, 4, 'arithmetic holds');
