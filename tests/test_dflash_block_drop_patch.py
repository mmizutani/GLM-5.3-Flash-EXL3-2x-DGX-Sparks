#!/usr/bin/env python3
"""overlay/patch_dflash_block_drop.py on copies of speculative.py / scheduler.py.

Runs inside the image at build time (site defaults) and on the host when
GLM53_SPECULATIVE_PY_SRC / GLM53_SCHEDULER_PY_SRC point at extracted copies.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PATCH = ROOT / "overlay" / "patch_dflash_block_drop.py"
SITE = Path("/usr/local/lib/python3.12/dist-packages/vllm")
SPEC_SRC = Path(os.environ.get("GLM53_SPECULATIVE_PY_SRC", SITE / "config/speculative.py"))
SCHED_SRC = Path(os.environ.get("GLM53_SCHEDULER_PY_SRC", SITE / "v1/core/sched/scheduler.py"))
MARK = "# [glm53-dflash-block-drop]"


def main() -> int:
    for src in (SPEC_SRC, SCHED_SRC):
        if not src.is_file():
            raise SystemExit(f"missing {src}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spec = root / "speculative.py"
        sched = root / "scheduler.py"
        shutil.copyfile(SPEC_SRC, spec)
        shutil.copyfile(SCHED_SRC, sched)
        env = os.environ.copy()
        env["GLM53_SPECULATIVE_PY"] = str(spec)
        env["GLM53_SCHEDULER_PY"] = str(sched)

        eagle_old = "return self.method in (\"eagle\", \"eagle3\", \"mtp\", \"dflash\", \"dspark\")"
        assert eagle_old in spec.read_text()

        subprocess.check_call([sys.executable, str(PATCH)], env=env)
        st = spec.read_text()
        ht = sched.read_text()
        assert "def use_eagle_block_drop(" in st
        assert "\"eagle\", \"eagle3\", \"mtp\"" in st
        assert eagle_old in st, "use_eagle semantics unchanged"
        assert st.count("def use_eagle_block_drop(") == 1
        assert st.count("def use_eagle_preserves_target_kv_cache(") == 1
        assert ht.count(MARK) == 3, "init + set + split"
        assert "if self.use_eagle_block_drop:  # [glm53-dflash-block-drop]" in ht
        compile(st, "speculative.py", "exec")
        compile(ht, "scheduler.py", "exec")

        subprocess.check_call([sys.executable, str(PATCH)], env=env)  # idempotent
        assert spec.read_text() == st
        assert sched.read_text() == ht

        shutil.copyfile(SCHED_SRC, sched)
        drifted = sched.read_text().replace(
            "        speculative_config = vllm_config.speculative_config\n"
            "        self.use_eagle = False\n",
            "        speculative_config = vllm_config.speculative_config\n",
        )
        sched.write_text(drifted)
        result = subprocess.run([sys.executable, str(PATCH)], env=env, capture_output=True, text=True)
        assert result.returncode != 0, "drifted scheduler must fail closed"
        assert "expected one" in result.stderr, result.stderr
    print("dflash block-drop patch OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
