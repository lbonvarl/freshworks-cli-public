from __future__ import annotations

import runpy
import unittest
from unittest import mock


class ModuleEntryPointTests(unittest.TestCase):
    def test_python_dash_m_exits_with_the_cli_return_code(self) -> None:
        # `python -m <package>` must propagate main()'s exit code, so a
        # failed command is visible to shells and scripts.
        for package in ("freshsales_cli", "freshservice_cli"):
            for code in (0, 1):
                with (
                    self.subTest(package=package, code=code),
                    mock.patch(f"{package}.cli.main", return_value=code),
                    self.assertRaises(SystemExit) as raised,
                ):
                    runpy.run_module(package, run_name="__main__")
                self.assertEqual(raised.exception.code, code)

    def test_importing_the_module_does_not_run_the_cli(self) -> None:
        for package in ("freshsales_cli", "freshservice_cli"):
            with (
                self.subTest(package=package),
                mock.patch(f"{package}.cli.main") as main,
            ):
                runpy.run_module(f"{package}.__main__", run_name="not_main")
            main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
