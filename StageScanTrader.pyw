# 雙擊這個檔案啟動「階段掃描下單」（獨立視窗，不會出現黑色命令視窗）。
# 需要先安裝：python -m pip install -U kgisuperpy pandas numpy pyyaml tzdata pywebview
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trader.desktop import main  # noqa: E402

sys.exit(main())
