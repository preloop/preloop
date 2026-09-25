"""``python -m preloop.cra`` runs the fail-closed CI helper.

``python -m preloop.cra measure <sbom>`` prints the platform's minimum-elements
measurement for those files. Any other invocation is the CI helper, unchanged.
"""

import sys


def main() -> int:
    """Dispatch ``measure`` or the CI helper.

    The measure path imports only ``sbom_measure``, which is stdlib. The CI
    helper is imported after that branch so a dependency-free SBOM job can
    run the measurement.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "measure":
        from preloop.cra.sbom_measure import main as measure_main

        return measure_main(sys.argv[2:])
    from preloop.cra.ci import main as ci_main

    return ci_main()


if __name__ == "__main__":
    raise SystemExit(main())
