"""Run on Linux with the real systemd parser. Does not register or start units."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from board_manage import render_unit, validate_unit

@unittest.skipUnless(sys.platform=='linux' and shutil.which('systemd-analyze'), 'Requires Linux systemd-analyze')
class SystemdParserTests(unittest.TestCase):
    def fixture(self, root):
        project=root/'project with spaces'
        (project/'autostart').mkdir(parents=True)
        (project/'autostart/board_runner.py').write_text('# inert parser fixture\n')
        return render_unit(project,sys.executable)
    def test_unquoted_working_directory_passes_actual_parser(self):
        with tempfile.TemporaryDirectory() as d:
            validate_unit(self.fixture(Path(d)))
    def test_old_quoted_working_directory_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as d:
            content=self.fixture(Path(d))
            line=next(x for x in content.splitlines() if x.startswith('WorkingDirectory='))
            bad=content.replace(line,'WorkingDirectory="'+line.split('=',1)[1]+'"')
            with self.assertRaises(SystemExit) as caught:validate_unit(bad)
            self.assertIn('not absolute',str(caught.exception))

if __name__=='__main__':unittest.main()
