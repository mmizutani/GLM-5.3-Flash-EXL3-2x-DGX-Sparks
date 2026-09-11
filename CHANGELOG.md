# Changelog

## 2026-09-11 (merge) — rebase onto origin (PR #130) with a measured conflict resolution

Merged `origin/main` (`9348755`, "per-KV-cache-group APC retention with DFlash
SWA replay") into the headroom branch. Upstream's launcher machinery is adopted
as-is (artifact gate, `GLM53_OVERLAY_ORDER`, retention knobs, tests, docs), but
the reworked retention/hybrid overlays measured **worse** on this kit's agent
probe than the #83 implementations this branch had already validated:
3×30 k repeats 0.000 vs 1.000 hits, 2×60 k 0.955 vs 0.999
(`docs/headroom-2026-09-11.md` §12.3, boots B4–B6; upstream itself frames #130
as a draft, measured on 128 k append/edit/branch rather than repeat growth).
The branch therefore keeps `overlay/patch_apc_per_group_retention.py` and
`overlay/patch_hybrid_prefix_hit.py` at the #83 semantics, plus:

- `overlay/patch_apc_fine_grained_hits.py` (`GLM53_FINEGRAINED_APC=1`, 64-token hits)
- `overlay/patch_dflash_block_drop.py` (vLLM #54163 cherry-pick)
- adaptive-k default + auto union capture list, spinwait 16
- dual-HCA GID preflight; portable jinja2 host check for the new
  chat-template validation (linuxbrew `python3` lacks jinja2, system python has it)

All 12 host suites pass, and B7 re-validated the restored stack live on the
merged launcher: 1×/3×/4× 30 k repeats 1.000 at 0.3–0.4 s (vs 0.000 for the
reworked overlays), 2×60 k 0.999, 3×5 k 0.992, 4-agent × 60 k turns 1–3
0.886–0.928 (wall 17.7–23.6 s; B3: 0.916–0.928).

## 2026-09-11 (later) — Prefix-cache cliff FIXED: adopted PR #83/#84 + vLLM #54163

The 8.7 % production hit rate from the morning investigation is fixed by adopting
the repo's P1 prefix-cache PRs and cherry-picking the matching upstream fix:

- **PR #83** `overlay/patch_apc_per_group_retention.py` + `GLM53_APC_RETENTION_INTERVAL_SWA`
  (default auto): per-KV-group retention. The EAGLE-exempt DFlash2 drafter keeps only
  reachable-boundary snapshots instead of 33 of the 38 block ids a cached 3584-token
  segment costs, so it stops evicting the MLA/mamba blocks that carry the hit.
- **PR #84** `overlay/patch_apc_fine_grained_hits.py` + `GLM53_FINEGRAINED_APC=1`
  (default): excludes the KpoolTail scratch from the fine-grained veto, restoring
  64-token hit alignment (was forced to the 3584-token page; warm agent turns
  recomputed up to 3583 tokens).
- **vLLM #54163 cherry-pick** `overlay/patch_dflash_block_drop.py`: `use_eagle()`
  includes `dflash`, so the scheduler took the EAGLE trailing-block back-off that
  DFlash never pollutes; the Mamba state never materialized at a block boundary and
  every reply recomputed the context (upstream #53477/#54094/#45238).
- Launcher hardening from the PRs: `validate_overlay_artifacts` fail-closed gate
  before `restart` stops anything, `GLM53_OVERLAY_ORDER` emitted identically to both
  ranks, `GLM53_FINEGRAINED_APC` 0/1 and `GLM53_APC_RETENTION_INTERVAL_SWA` grid
  validation. Host tests all pass.

**Live A/B on boot B3** (same probes as the morning run, `.env` as now shipped:
adaptive-k `ema`, spinwait 16, dense FP8, both new knobs defaulted):

| Probe | before | after |
|---|---|---|
| N=3 × 30 k repeat | 17.2 s each, 0.000 hits | **0.4 s each, 1.000** |
| N=4 × 30 k repeat | 17.2 s each, 0.000 | **0.4 s each, 1.000** |
| 2 × 60 k repeat | 34–35 s, 0.000 | **0.4 s, 0.999** |
| 4-agent × 60 k turns 1–3 | 0.000, wall 150–196 s | **0.916–0.928, wall 17.7–18.9 s** |
| hash-map prose | 34.43 tok/s | 34.50 (no regression), coherence pass |

Details and receipts: `docs/headroom-2026-09-11.md` §12,
`logs/headroom-20260911/B3-*`. Open upstream items: **#54076** (mamba block-grid
chunk split) and **#55601** (state-seed units) — neither blocks this fix.

## 2026-09-11 — adaptive-k default ON, spinwait 16, long-prefill warmup; prefix-cache cliff found

Live re-measure on this 2× GB10 kit (three clean boots B0/B1/B2), full write-up
`docs/headroom-2026-09-11.md`, raw receipts `logs/headroom-20260911/`:

- **`GLM53_ADAPTIVE_K=ema` is now the `.env.example` default.** A/B/A (union captures, MNBT 7168,
  spinwait 2): hash-map prose 29.36 vs 26.63 tok/s (**+10.3 %**), structured 65.95 vs 65.91
  (neutral). `start.sh` now auto-adds the required capture list `1 2 3 4 5 6 8 9 10 12 15 16 20 24 32`
  when the knob is `ema/on/1`; previously stock `1 2 4 8 16 24 32` silently missed the 3- and
  5-token graph shapes. The union list costs ~51 k KV tokens (984,210 → 933,082 at 0.85).
- **`GLM53_SPINWAIT_MS=16`** in `.env.example` (was `stock`). Re-checked at MNBT 7168: no
  regression (prose 30.44 tok/s), consistent with the frozen 2048 sweep (+0.95 %, −85 % CPU).
- **Dense FP8 stays opt-in** but is now fully measured: stacked vs stock k=7 + BF16 dense,
  structured 65.9 → 75.0 tok/s, prose 26.6 → 34.4; KLD 0.002–0.016 nats / argmax 97.0–99.7 %
  on four fixed texts (one repetitive-prose text above the old 0.013 proxy bar).
- **Boot warmup gap fixed** (`scripts/boot-shape-warmup.sh`): the only mid-serve JIT in the prior
  5 h serve was `BuildPrefillChunkMetadataKernel`; added `PREFILL_S=(3584 7168 14336 65536)`,
  staged long payloads through a file (ARG_MAX), and ignored payload files in the outcome tally.
- **Prefix-cache cliff diagnosed**: the live 8.7 % hit rate is server-side. Replaying the exact
  deep-swe agent payloads hits on an idle engine where the live run missed; a sequential probe
  shows 1–2 long sessions retain prefixes (~96–100 %) and 3+ lose everything, independent of the
  918 k-token pool, adaptive-k, and `VLLM_PREFIX_CACHE_RETENTION_INTERVAL=3584` (knob plumbed
  through `start.sh`, left unset). Recommended mitigation: keep simultaneously cached long
  sessions ≤ 2.
- **Dual-rail CX7 verified**: single HCA 12.84 GB/s vs dual 20.94 GB/s peak all-reduce busbw;
  `NCCL_CROSS_NIC=1` / `NCCL_IB_MERGE_NICS=1` gave no further gain.
- Host DRAM is at the ceiling (2 GiB MemAvailable under concurrent 40 k prefills even with the
  agent sandboxes stopped): do not raise `GPU_MEM_UTIL`. New probes: `tests/bench_agent_sessions.py`,
  `tests/replay_responses_traj.py`, `tests/bench_logprobs.py`.

## 2026-09-07 — E3 grouped fat-expert MoE prefill (`EXL3_FAT_GROUPED`, now the default)

Cold prefill **+37–45%** on this 2× GB10 kit (16k: 1,155 → 1,578 tok/s; 128k: ~1,150 → 1,629; 256k: 1,087 → 1,576),
decode unchanged. Commit `1a0feb0` (merge `dfd8e0f`); made the launcher default later the same day together with `MAX_MODEL_LEN` 1M → 900k, `GPU_MEM_UTIL` 0.87 → 0.85, `GLM53_INDEXER_WORKSPACE` stock → rightsize, and `EXL3_TEMP_ROWS_FUSED` 128 → 32 (E3) / 256 (E2), in `start.sh` and `.env.example`. Kernel design from the Fable prototype
(`.claude/worktrees/fable-perf`), qualified and measured in `logs/overnight-20260906T164059Z/`.

### What was slow before (E2, `EXL3_FAT_KERNEL=1`, cap 256)

Every prefill chunk (7,168 tokens × top-8 = ~57k token→expert routes per layer, 42 MoE layers) split experts
into *thin* (≤ cap rows, one fused `exl3_moe` launch for all of them) and *fat* (> cap rows). Fat experts went
through a **host-driven loop, one expert at a time**:

1. one D2H copy + sync of the routing counts to learn which experts were fat;
2. per fat expert: `index_select` the rows, input Hadamard, **copy the gate and up trellises into a stacked
   scratch (4 MB)** plus their output scales, launch the direct GEMM, then five separate elementwise kernels
   (two clamps, sigmoid, two multiplies), an fp16 copy, the down-input Hadamard, the down GEMM + scatter —
   about **14 launches and ~0.3–0.5 ms of host work per expert**.

With real routing most of the 288 experts in a layer are fat in a 7,168-token chunk, so a layer spent most
of its ~80–90 ms waiting for the CPU to feed the next expert; lowering the cap made it *worse* (cap 32:
113 ms), because more experts fell into that loop. MoE was roughly half of chunk time.

### What E3 does instead (`overlay/exl3_fat_moe.cu`, `overlay/exl3.py`)

- **Device-side segment tables.** From the sorted routing counts, ~20 small torch ops build, on the GPU,
  a row table (fat row → token, expert, route weight) and a segment table (64-row tile → expert, first row,
  rows). The kernels read the live `num_rows` / `num_segs`, so **no host synchronization** on routing and
  the layer stays CUDA-graph capturable.
- **Three launches cover every fat expert of the layer:**
  1. `gather`: fat rows → contiguous buffer, input scale + Hadamard applied;
  2. `gateup`: 64-row × 128-column tiles, 4-stage `cp.async` pipeline, trellis tiles **dequantized once per
     16 K per warp and reused across all M blocks**, gate and up streams in the same tile; the epilogue fuses
     both output Hadamards, the SwiGLU clamp/activation, and the down-input Hadamard, writing fp16;
  3. `down`: same mainloop over the intermediate, output Hadamard + route weight, **16-byte vector
     `atomicAdd(float4)` scatter** into the fp32 output, so tiles of different experts run concurrently.
- Net effect per layer: hundreds of launches and hundreds of 4 MB weight copies → 3 launches + table build.
  Isolated 7,168-token layer: **77–91 ms (E2 cap 256) → 31 ms (E3 cap 32)**, 2.1–3.0× across routing skews,
  at 46 TFLOPS vs 16–19. That is where the +38% end-to-end comes from (MoE ≈ half of chunk time).

### What changed relative to the prototype so it could ship

- **E2 rounding boundaries restored**: input scale multiplied in fp16 before the fp32 Hadamard; SiLU with
  precise `expf`/division (module built without `--use_fast_math`, unlike exllamav3); activation rounded to
  fp16 and multiplied by `down.suh` in fp16 before the fp32 down-input Hadamard. E3's error vs the LinearEXL3
  reference is now identical to E2's on every metric (incl. real checkpoint experts); the remaining E3/E2
  difference is the atomic accumulation order (max 0.125 on outputs ~4,600).
- **Load-time eligibility** (K4/MCG, no `mul1`, shared gate/up SUH, hidden % 256, intermediate % 128,
  sm_90+, single device) with a visible fallback to the E2 tier; grouped requested without the kernels
  **fails closed** at boot; diag schema 2; scratch growth refused during graph capture; the fused cap is
  never changed implicitly (set `EXL3_TEMP_ROWS_FUSED=32` explicitly, keep it ≥ `MAX_NUM_SEQS × (DFLASH_TOKENS+1)`).
- Tests: table builder vs host reference, parity vs loop and E2 under frozen tolerances, value regimes, real
  checkpoint experts, graph replay with changed data, scratch growth, invalid routes, fallbacks
  (`tests/test_exl3_overlay.py`); layer bench `tests/bench_e3_microbench.py`.
- Build: `Dockerfile.e3-layer` + `overlay/build_exl3_fat_moe_ext.py` compile only the new translation unit
  onto the existing image (tested); the full `Dockerfile` path now also installs the sources (not yet exercised).

### Known limitation

E3 keeps a persistent fat-row scratch (`h13` 448 MiB + `h2` 112 MiB for 57,344 rows) that is allocated during
vLLM's profile run and therefore charged to the KV budget (−0.56 GiB, −1…4% of the pool depending on util).
At 1M context that removes the single-request capacity on this kit, so the shipped defaults are
`MAX_MODEL_LEN=900000`, `GPU_MEM_UTIL=0.85`, `GLM53_INDEXER_WORKSPACE=rightsize` (measured recipe: 500k / 0.84;
900k / 0.87 served a 256k prefill with driver retries). Fix path: fuse the gather
into the gate/up A-tile load (drops `h13`) or size scratch from actual fat rows. Prompts ≥ ~100k tokens
remain close to the head's host-memory limit at any util; a 256k prefill at util 0.87 with zero MemAvailable
crashed the head on 2026-09-06.

Also in the same change: `MAX_MODEL_LEN` caller override in `start.sh`, effective-EXL3-knobs boot line,
`.env.example` docs. Not included: the DFlash2 vocab-parallel top-k experiment (inconclusive, worktree only).
