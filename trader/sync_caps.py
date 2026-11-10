"""把 trader/kgi_spec.py 的選項表寫回 index.html（網頁未連線時用來停用 API 不支援的選項）。

修改 kgi_spec.py 後執行：python -m trader.sync_caps
"""

import json
import re
from pathlib import Path

from . import kgi_spec

INDEX = Path(__file__).resolve().parents[1] / "index.html"


def main() -> None:
    html = INDEX.read_text(encoding="utf-8")
    caps = json.dumps(kgi_spec.capabilities(), ensure_ascii=False)
    new, n = re.subn(r'(<script type="application/json" id="caps">)(.*?)(</script>)',
                     lambda m: m.group(1) + caps + m.group(3), html, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("index.html 找不到 caps 區塊")
    INDEX.write_text(new, encoding="utf-8")
    print("已更新 index.html 的選項表")


if __name__ == "__main__":
    main()
