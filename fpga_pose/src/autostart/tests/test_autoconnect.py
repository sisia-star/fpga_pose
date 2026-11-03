import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from private_token import load_or_create, read_token
from board_manage import render_unit


@unittest.skipUnless(os.name == "posix", "Board tests require POSIX")
class AutoconnectTests(unittest.TestCase):
    def test_token_is_reused_after_module_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private'/'pairing.token'
            a=load_or_create(p)
            self.assertEqual(a, load_or_create(p))
            self.assertEqual(a, read_token(p))
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode),0o700)
            self.assertEqual(len(a),43)

    def test_invalid_file_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'pairing.token';p.write_text('invalid');p.chmod(0o600)
            with self.assertRaises(ValueError):load_or_create(p)
            self.assertEqual(p.read_text(),'invalid')

    def test_world_readable_token_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'pairing.token';load_or_create(p);p.chmod(0o644)
            with self.assertRaises(ValueError):read_token(p)

    def test_symlink_token_and_directory_refused(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);p=d/'original';load_or_create(p)
            link=d/'link';link.symlink_to(p)
            with self.assertRaises(OSError):read_token(link)
            sub=d/'linked_dir';sub.symlink_to(d,target_is_directory=True)
            with self.assertRaises(ValueError):load_or_create(sub/'pairing.token')

    def test_virtualenv_python_symlink_not_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);link=d/'venv'/'bin'/'python3';link.parent.mkdir(parents=True)
            link.symlink_to(sys.executable)
            service=render_unit(d/'My Project',link)
            self.assertIn('"'+str(link)+'" "-u"',service)
            self.assertIn('WorkingDirectory='+str((d/'My Project').resolve())+'\n',service)
            self.assertNotIn('WorkingDirectory=\"',service)
            self.assertIn('XILINX_XRT=/usr',service)
            self.assertIn('WantedBy=multi-user.target',service)
            self.assertNotIn('DEEPSEEK_API_KEY',service)

    def test_unit_percent_expansion_and_newline_rejected(self):
        unit=render_unit('/tmp/pose%project','/usr/bin/python3')
        self.assertIn('pose%%project',unit)
        with self.assertRaises(ValueError):render_unit('/tmp/a\nExecStart=bad','/usr/bin/python3')

    def test_manual_relay_compatible_with_saved_board_token(self):
        training=ROOT.parents[1]/'fpga_pose_training_20261025'
        if not (training/'ai_coach.py').is_file():
            self.skipTest('Optional original project fixture not present')
        sys.path.insert(0,str(training))
        from ai_coach import AICoachBroker
        with tempfile.TemporaryDirectory() as d:
            token=load_or_create(Path(d)/'pairing.token')
            for _ in range(2):
                broker=AICoachBroker(provider='relay');broker.pairing_token=token
                self.assertEqual(broker.relay_poll(token),{'job':None})
                self.assertNotIn(token,str(broker.snapshot()))
                broker.close()


class FakeServer:
    instances=[]
    def __init__(self,**kw):
        self.ai_coach=types.SimpleNamespace(pairing_token='temporary')
        self.kw=kw;self.port=kw['port'];self.stopped=False
        type(self).instances.append(self)
    def start(self):
        if self.ai_coach.pairing_token=='temporary':raise AssertionError('Token applied too late')
    def status(self):return {'ready':True,'running':True,'frame_id':1}
    def stop(self):self.stopped=True


@unittest.skipUnless(os.name == "posix", "Board tests require POSIX")
class RunnerTests(unittest.TestCase):
    def test_token_set_before_start_and_shutdown_stops_owner(self):
        import board_runner
        class Event:
            def __init__(self):self.calls=0
            def is_set(self):return False
            def set(self):pass
            def wait(self,_):self.calls+=1;return True
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'fpga_pose_server.py').write_text('# fixture')
            fake=types.SimpleNamespace(FPGAPoseServer=FakeServer)
            with patch.object(sys,'argv',['board_runner','--project',str(root),'--token-file',str(root/'private/pairing.token'),'--camera',str(root/'not-a-device')]),patch.dict(sys.modules,{'fpga_pose_server':fake}),patch.object(board_runner.threading,'Event',Event),patch.object(board_runner.signal,'signal'),patch.object(board_runner.os,'chdir'):
                board_runner.main()
            self.assertTrue(FakeServer.instances[-1].stopped)
            self.assertEqual(FakeServer.instances[-1].ai_coach.pairing_token,read_token(root/'private/pairing.token'))


if __name__=='__main__':unittest.main()
