"""The version lives in puckutility/version.py and shows as v<version>."""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from puckutility import __version__, version  # noqa: E402
from puckutility.cli import build_parser  # noqa: E402


class TestVersion(unittest.TestCase):

    def test_one_pep440_version(self):
        self.assertEqual(version.__version__, '1.4.1')
        self.assertIs(__version__, version.__version__)

    def test_cli_version_and_help_show_it(self):
        parser = build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser.parse_args(['--version'])
        self.assertEqual(out.getvalue().strip(), 'puckutility v1.4.1')
        self.assertIn('puckutility v1.4.1', parser.format_help())

    def test_title_bar_shows_it_after_the_name(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', 'puckutility', 'gui', 'app.py')) as fh:
            source = fh.read()
        self.assertRegex(source, r"SetTitle\([\"'][^\"']+ v[\"'{]")

    def test_pyproject_reads_version_py(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', 'pyproject.toml')) as fh:
            self.assertIn('attr = "puckutility.version.__version__"', fh.read())


if __name__ == '__main__':
    unittest.main()
