# Synthetic TAP fixture: must make prove exit non-zero.
use strict;
use warnings;
use Test::More tests => 1;

is(2 + 2, 5, 'deliberately wrong');
