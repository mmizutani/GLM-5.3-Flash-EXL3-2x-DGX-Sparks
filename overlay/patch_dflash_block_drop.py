#!/usr/bin/env python3
"""DFlash/DSpark must not take the EAGLE trailing-block prefix-cache back-off.

Cherry-pick of vLLM PR #54163 (fixes vLLM #53477; merged upstream as
`use_eagle_block_drop`) against the pinned image.

``SpeculativeConfig.use_eagle()`` is a stand-in for "spec decode that reads
target hidden states" and returns True for ``dflash``/``dspark`` too. The
scheduler used that bit to back ``last_cache_position`` off by one mamba block
in ``_mamba_block_aligned_split``. Only the eagle family pollutes the target's
last matching full-attention block with its lookahead KV write; DFlash/DSpark
draft from their own KV cache and never write target blocks. The spurious
back-off made prompts shorter than two mamba blocks skip the final
block-aligned chunk, so the Mamba recurrent state never materialized on a
block boundary and the next turn's fixed-point lookup converged to 0 — the
whole context was recomputed on every reply.

This patch adds ``use_eagle_preserves_target_kv_cache()`` and
``use_eagle_block_drop()`` to SpeculativeConfig, propagates the bit into the
scheduler, and uses it for the mamba split back-off. ``use_eagle`` itself is
unchanged: lookahead shifts and speculative block handling keep their
semantics.

Fail closed if the pinned anchors drift; idempotent via MARK.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SPEC = Path(
    os.environ.get(
        "GLM53_SPECULATIVE_PY",
        "/usr/local/lib/python3.12/dist-packages/vllm/config/speculative.py",
    )
)
SCHED = Path(
    os.environ.get(
        "GLM53_SCHEDULER_PY",
        "/usr/local/lib/python3.12/dist-packages/vllm/v1/core/sched/scheduler.py",
    )
)
MARK = "# [glm53-dflash-block-drop]"

SPEC_ANCHOR = """    def use_eagle(self) -> bool:
        # NOTE: This method is usually a stand-in for "speculative decoding using
        # target model hidden states"
        # TODO(ben): Refactor this so the naming is clearer
        return self.method in ("eagle", "eagle3", "mtp", "dflash", "dspark")

    def use_dflash(self) -> bool:
"""

SPEC_REPLACEMENT = """    def use_eagle(self) -> bool:
        # NOTE: This method is usually a stand-in for "speculative decoding using
        # target model hidden states"
        # TODO(ben): Refactor this so the naming is clearer
        return self.method in ("eagle", "eagle3", "mtp", "dflash", "dspark")

    def use_eagle_preserves_target_kv_cache(self) -> bool:
        # Only eagle-family drafters share (and pollute via lookahead KV
        # write) the target's full-attention KV cache groups. DFlash/DSpark
        # draft from their own KV cache and never write target blocks.
        return self.method in ("eagle", "eagle3", "mtp")

    def use_eagle_block_drop(self) -> bool:
        \"\"\"Whether volatile trailing cache blocks should be discarded.

        DFlash/DSpark never write target blocks (vLLM #53477), so the
        one-block prefix-cache back-off must not apply to them.
        \"\"\"
        return self.use_eagle_preserves_target_kv_cache()

    def use_dflash(self) -> bool:
"""

INIT_OLD = """        speculative_config = vllm_config.speculative_config
        self.use_eagle = False
"""
INIT_NEW = """        speculative_config = vllm_config.speculative_config
        self.use_eagle = False
        self.use_eagle_block_drop = False  # [glm53-dflash-block-drop]
"""

SPEC_SET_OLD = """            self.use_eagle = speculative_config.use_eagle()
"""
SPEC_SET_NEW = """            self.use_eagle = speculative_config.use_eagle()
            self.use_eagle_block_drop = (
                speculative_config.use_eagle_block_drop()
            )  # [glm53-dflash-block-drop]
"""

DROP_OLD = """        last_cache_position = request.num_tokens - request.num_tokens % block_size
        if self.use_eagle:
            last_cache_position = max(last_cache_position - block_size, 0)
"""
DROP_NEW = """        last_cache_position = request.num_tokens - request.num_tokens % block_size
        if self.use_eagle_block_drop:  # [glm53-dflash-block-drop]
            last_cache_position = max(last_cache_position - block_size, 0)
"""


def replace_once(path: Path, text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected one {label} target, found {text.count(old)}")
    return text.replace(old, new, 1)


def patch_speculative() -> bool:
    if not SPEC.is_file():
        raise SystemExit(f"missing {SPEC}")
    text = SPEC.read_text()
    if "def use_eagle_block_drop(" in text:
        print(f"{SPEC.name}: use_eagle_block_drop already present — skipping")
        return False
    text = replace_once(SPEC, text, SPEC_ANCHOR, SPEC_REPLACEMENT, "use_eagle")
    SPEC.write_text(text)
    print(f"patched {SPEC.name} (use_eagle_block_drop: dflash/dspark exempt)")
    return True


def patch_scheduler() -> bool:
    if not SCHED.is_file():
        raise SystemExit(f"missing {SCHED}")
    text = SCHED.read_text()
    if MARK in text:
        print(f"{SCHED.name}: {MARK} already present — skipping")
        return False
    text = replace_once(SCHED, text, INIT_OLD, INIT_NEW, "use_eagle init")
    text = replace_once(SCHED, text, SPEC_SET_OLD, SPEC_SET_NEW, "use_eagle set")
    text = replace_once(SCHED, text, DROP_OLD, DROP_NEW, "mamba split back-off")
    SCHED.write_text(text)
    print(f"patched {SCHED.name} (mamba split uses use_eagle_block_drop)")
    return True


def main() -> int:
    patch_speculative()
    patch_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
