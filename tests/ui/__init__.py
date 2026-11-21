"""页面测试（见 conftest.py 开头）。

这个文件夹是一个包（有这个 __init__.py），这样它的 conftest 叫 ui.conftest，不会和 tests/conftest.py 撞名：
别的测试里有 `from conftest import RecordingUI`，要是这里的 conftest 也叫 conftest，它们就会拿错文件。
"""
