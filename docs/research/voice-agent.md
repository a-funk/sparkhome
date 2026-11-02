# SparkHome — Local voice + agent + computer-use stack on DGX Spark (GB10)

Research date: **2026-10-08** (task dated 2026-10-07). Author: research subagent. Audience: later build agents.
Scope: (1) small tool-calling LLMs, (2) ASR, (3) wake word, (4) TTS, (5) computer-use / GUI-agent models,
(6) harness design incl. a minimal MCP server, (7) GPU/memory contention with games; plus a recommended stack,
an end-to-end latency budget (button-up → action on screen), a fallback plan and an incremental delivery plan.

**Evidence labels used everywhere:**
- **[M] measured** — measured by me in this session (on a Mac, not the Spark) or measured by the user on the Spark (stated in the task).
- **[D] documented** — vendor/spec/primary docs (model cards, specs, official READMEs).
- **[R] reported** — third-party measurement/claim (forums, blogs, aggregators, papers by others).
- **[I] inferred** — my reasoning/arithmetic; must be measured before relying on it.

Machine facts from the task (measured by the user on 2026-10-07, not re-verified): GB10 (10× X925 + 10× A725),
121.6 GiB usable unified LPDDR5x (~208 GB/s measured), DGX OS 7.2.3 / Ubuntu 24.04.4 arm64, kernel 6.17.0-1029-nvidia,
driver 580.173.02, GNOME on Xorg, GDM autologin user `spark` (DISPLAY=:1), Sony TV 1080p60 over HDMI-0, PipeWire,
Chrome 155 arm64 + Widevine, MT7925 BT (BlueZ 5.72; Xbox Series controller pairs over BLE), Steam snap + FEX + Proton,
whisper.cpp CUDA large-v3-turbo-q8_0, Piper en_US-lessac-medium, Ollama (gemma3:1b only), vLLM containers for
Qwen3-4B-Instruct-2507 and Nemotron 3.5 Lightning 30B-A3B NVFP4 (**101.6 tok/s decode, measured by user**),
Node 22.23.1, Python 3.12.3. No microphone attached. `spark` not in `input` group; in `docker` group; no passwordless sudo.

---

## 0. TL;DR — decisions for the build

1. **Do not make a pixel "computer-use" model the primary way an agent drives the launcher.** We own the launcher, so
   expose it as **semantic tools** (list/search/open tile, focus, navigate, media, volume, launch game) plus a JSON
   **UI-state tree** (what is on screen, which tile has focus). Evidence: on the only TV/D-pad agent benchmark
   (TVWorld, ACL Findings 2026) general GUI agents collapse — UI-TARS-1.5-7B **1.6%**, OpenCUA-7B **5.0%**,
   Qwen3-VL-8B **20.0%**, Qwen3-VL-32B **39.0%** task success; a fine-tuned 8B reached 68.3% **[R]**. Pixel CUA on the Spark costs
   **3.3–6.8 s per step** even for the best-optimized local model (Holo3.1-35B-A3B, H Company's Spark numbers **[D]**).
2. **Voice = push-to-talk first, wake word later.** PTT removes the endpointing delay, the TV-audio false-trigger problem,
   and the need for acoustic echo cancellation. Porcupine's free tier is gone (keys disabled after 2026-06-30 **[R]**).
3. **Pipeline:** PTT audio → ASR → **deterministic fast path** (regex grammar + fuzzy title match; ~3 ms **[M]**) →
   only if no match: **local LLM tool call** (vLLM, thinking **off**, `tool_choice="required"`) → launcher action.
   Expected button-up→action: **~0.2–0.45 s** fast path, **~0.7–1.1 s** LLM path **[I from measured parts]**.
4. **LLM:** keep the already-running **Nemotron 3.5 Lightning 30B-A3B NVFP4** on vLLM (user-measured 101.6 tok/s;
   81 tok/s bare / 124 tok/s with DSpark on Spark **[R]**). A/B it against **Qwen3.6-35B-A3B NVFP4** (77 bare / 108 MTP
   tok/s on Spark **[R]**, Apache-2.0, stronger agentic scores **[D]**) on a SparkHome-specific eval set. On GB10, a
   3B-active MoE in 4-bit decodes faster than a dense 4B in BF16 (bandwidth-bound) **[I]**. Ollama is fine for experiments
   but slower to first token (161–511 ms vs 94–131 ms for vLLM on Spark **[R]**) and does not batch.
5. **ASR:** start with the **already-installed whisper.cpp large-v3-turbo (CUDA)** behind `whisper-server` with Silero VAD
   and an initial prompt listing tile names; measure. Upgrade path: **Parakeet-TDT-0.6B-v2** (English, WER 6.05 vs
   ~7.6–7.8 for turbo on the Open ASR board **[R]**) on **CPU via onnx-asr** (no GPU contention with games) or GPU via NeMo
   with GPU-PB phrase boosting; **Nemotron Speech Streaming EN 0.6B** (tested on DGX Spark per its card **[D]**) if live
   captions while holding the button are wanted.
6. **TTS is off the critical path.** Visual feedback first; Piper (installed) for optional short confirmations; never speak
   during games/video unless asked.
7. **MCP:** expose the same tool set over **one Streamable-HTTP endpoint that is dual-era** (MCP **2026-07-28** stateless
   + legacy **2025-11-25** initialize handshake). A tested stdlib reference is at `scratchpad/research/mcp_min.py`; it
   interoperates with the official Python SDK 2.3.0 and TypeScript SDK 2.3.1 clients in auto / pinned-modern / legacy
   modes **[M]**. Connect Claude Code with `claude mcp add --transport http sparkhome http://127.0.0.1:8765/mcp`
   (through an SSH tunnel from the Mac).
8. **Contention:** the big risk is not capacity but **memory bandwidth during active decode** (a 3B-active NVFP4 decode at
   ~100 tok/s streams roughly 150+ GB/s — most of the 208 GB/s **[I]**). Policy: while a game is foreground, voice uses
   the fast path only; LLM calls require confirmation (or accept a short stutter). Cut vLLM's KV reservation
   (`--gpu-memory-utilization` ≈0.25–0.35 instead of the recipe's 0.85–0.91) so games have RAM **[R/I]**. Pin voice
   services to the A725 cores **[I]**.

---

## 1. Recommended stack (v1) and architecture

| Layer | v1 choice (what to build first) | Why | Upgrade / alternative |
|---|---|---|---|
| Voice input | **Push-to-talk** button on remote/gamepad; USB mic (or a voice remote that enumerates as a USB-audio mic — must be tested) | No endpointing delay; no TV-audio false triggers; no AEC needed | Phone PWA hold-to-talk over LAN (needs HTTPS for `getUserMedia`); far-field mic + wake word later |
| VAD | Silero VAD v6.2 inside whisper.cpp (`--vad -vm ggml-silero-v6.2.0.bin`) to drop empty/noise presses | <1 ms per 32 ms chunk on CPU **[D/R]**; stops whisper hallucinating on silence | Silero ONNX standalone if ASR changes |
| ASR | **whisper.cpp large-v3-turbo-q8_0, CUDA, `whisper-server`** with `--prompt "<tile names>"`, `-l en` | Already installed and working; zero new deps | **Parakeet-TDT-0.6B-v2** (onnx-asr on CPU, or NeMo on GPU with GPU-PB word boosting); **Nemotron Speech Streaming EN 0.6B** (NeMo-Speech.cpp GGUF) for live captions |
| NLU fast path | Regex templates + fuzzy title resolver (`fastpath.py` reference, stdlib) | ~3 ms over 513 titles **[M, Mac]**; deterministic; ambiguous → show top-3 on screen | rapidfuzz if the catalog grows; embedding router (small embedding model) for paraphrases |
| LLM fallback | **vLLM + Nemotron 3.5 Lightning 30B-A3B NVFP4** (already running), thinking off, `tool_choice="required"`, prefix caching | Fastest measured local tool-capable model on this box | **Qwen3.6-35B-A3B NVFP4** (A/B); Qwen3.5-4B/9B (tools + vision) small fallback; Ollama for experiments |
| Tools | Semantic launcher API + UI-state tree; same definitions served to the local loop and over MCP | Fast, reliable, testable; works for Claude Code and local models | Add CDP/DOM tools for Chrome-hosted streaming apps |
| External agent API | **MCP Streamable HTTP, dual-era**, bound to 127.0.0.1, SSH tunnel for remote clients | Spec-compliant; Claude Code v2 runtime negotiates 2026-07-28 **[D]** | Official TS SDK v2 server (`@modelcontextprotocol/server` + express adapter) if the backend is Node |
| Browser apps | Chrome with `--remote-debugging-port=9222 --user-data-dir=<dedicated profile>`; agent reads DOM/AX via CDP, sends keys | Chrome ≥136 ignores the debug port on the default profile **[D]**; DOM is far cheaper than pixels | Playwright MCP `--cdp-endpoint` for external agents |
| Pixel CUA (optional, last) | **Holo3.1-4B/9B** (Apache-2.0) or **Qwen3.5-9B** native visual agent; background tasks only | Only for third-party UIs with no DOM/API | Holo3.1-35B-A3B NVFP4 (3.3 s/step on Spark **[D]**); Fara1.5 (MIT, browser-only) |
| TTS (optional) | **Piper** (installed), CPU, short confirmations | Fast on CPU, no GPU contention | Kokoro-82M (nicer, GPU) |
| Wake word (phase 2) | openWakeWord (ONNX runtime on py3.12) or microWakeWord (pymicro-wakeword) + PipeWire echo-cancel with `monitor.mode` | Porcupine free tier discontinued **[R]** | Train a custom "Hey Spark" with Piper-synthesized clips |

```
 Remote/gamepad PTT ──► input daemon ──► start/stop capture (PipeWire, 16 kHz mono, 300 ms pre-roll ring buffer)
                                              │ button-up (+~100 ms tail)
                                              ▼
                                   ASR (whisper-server / Parakeet) ──► transcript (shown on screen)
                                              ▼
                       fast path (regex + fuzzy titles) ──match──► Launcher Action API ──► UI (focus/launch)
                                              │ no match / ambiguous-free text            ▲
                                              ▼                                          │
                       LLM tool call (vLLM, thinking off, tool_choice=required) ─────────┘
                                              │ (multi-step tasks only)
                                              ▼
                       agent loop over: UI-state tree, CDP DOM snapshots, (last resort) screenshots
 External agents (Claude Code, etc.) ──► MCP endpoint (dual-era) ──► same Launcher Action API
```

---

## 2. End-to-end latency budget (button-up → action visible on the TV)

Assumptions: 2–5 s spoken command, push-to-talk, 1080p60 TV, launcher already running, LLM resident and warm.

### Path A — fast path (no LLM), the common case

| Stage | p50 | p95 | Evidence |
|---|---|---|---|
| Button-up event reaches app (BLE HID) | ~10 ms | ~20 ms | **[I]** BLE connection intervals are typically 7.5–15 ms |
| Tail capture after release (avoid clipping last phoneme) | 100 ms | 150 ms | design knob **[I]**; tune 0–200 ms |
| ASR final transcript, 3 s audio | whisper-turbo GPU **~150–400 ms**; Parakeet GPU **~20–60 ms**; Parakeet CPU (onnx-asr) **~80–250 ms**; streaming Nemotron **~80–160 ms after release** | ×1.5–2 | **[I]**, see §5; onnx-asr reports RTFx 36 on a Ryzen 9800X3D CPU for Parakeet v2/v3 **[R]**; whisper encoder processes a 30 s window unless `-ac` is reduced **[D]** |
| Fast-path match | ~3 ms | ~5 ms | **[M]** `fastpath.py`, 513 titles, difflib, Mac (M-series) |
| Dispatch + UI render | ~25 ms | ~50 ms | **[I]** 1.5–3 frames @60 Hz |
| TV processing | ~15 ms (Game mode) | ~100 ms (other picture modes) | **[I]**; check the Sony's picture mode |
| **Total** | **~0.18–0.55 s** (ASR-dependent) | **~0.3–0.9 s** | target **p50 ≤ 350 ms, p95 ≤ 600 ms** |

### Path B — LLM tool call (free-form phrasing, questions, multi-intent)

| Stage | p50 | p95 | Evidence |
|---|---|---|---|
| Path A up to transcript | ~150–450 ms | | above |
| Prompt: static system + tool schemas (prefix-cached) + dynamic UI state at the end (~200–600 new tokens) | TTFT **~100–200 ms** | ~300 ms | vLLM TTFT 94–131 ms on Spark for 3–4B-active models **[R]**; Ollama 161–511 ms **[R]**; prefill ~6,000 tok/s flat to 27k tokens **[R]** |
| Decode one tool call (~30–60 tokens, thinking OFF) | **~0.3–0.5 s** | ~0.75 s | Nemotron 3.5 Lightning NVFP4 on Spark: 81.3 tok/s bare, 111.4 MTP, 124.2 DSpark (c=1) **[R]**; user measured 101.6 **[M-user]** |
| Parse + execute tool + render | ~30–60 ms | ~100 ms | **[I]** |
| **Total** | **~0.7–1.1 s** | **~1.6 s** | target **p50 ≤ 1.0 s** |

Leaving reasoning ("thinking") on adds hundreds to thousands of tokens (**2–20 s**) **[I]** — the Qwen3.5-9B, Qwen3.6 and
Nemotron 3.5 Lightning cards say thinking is ON by default; disable with `chat_template_kwargs: {"enable_thinking": false}`
**[D]** (Unsloth's docs claim the small Qwen3.5 models default to OFF **[R]** — conflicting, so always set it explicitly).

### Path C — browser DOM agent step (e.g. find a show inside a streaming web app)
Per step ≈ LLM call with an accessibility/DOM snapshot of 1–3k tokens (~0.6–1.2 s **[I]**) + CDP action
(tens to hundreds of ms; a browser-use CLI page cites ~50 ms per call, a cloud benchmark ~570 ms per click **[R]**)
→ **~1–2.5 s per step**, 2–5 steps per task.

### Path D — pixel computer-use step (last resort)
H Company on DGX Spark, Holo3.1-35B-A3B: **average step 6.8 s (FP8) → 3.3 s (NVFP4 + NVIDIA harness optimizations)**;
18.1 agent requests/min best case **[D]**. Smaller 4B/9B models: ~2–4 s/step **[I]**. Use only for background tasks.

### Measurement plan (do this before trusting any [I] number)
```bash
# ASR latency on 3 s clips (whisper.cpp already built). Record 20 real commands first.
./build/bin/whisper-server -m models/ggml-large-v3-turbo-q8_0.bin -fa -l en -nf -sns \
  --vad -vm models/ggml-silero-v6.2.0.bin --prompt "Netflix, YouTube, Plex, Steam, Cyberpunk 2077, ..." \
  --host 127.0.0.1 --port 8090
for f in cmds/*.wav; do curl -s -o /dev/null -w "%{time_total}\n" 127.0.0.1:8090/inference \
  -F file=@$f -F temperature=0.0 -F response_format=json; done | sort -n
# try also: -ac 256 (≈5 s of audio context) and compare WER on the same clips

# LLM TTFT + total for a tool call (vLLM OpenAI API, streaming to time first token)
python3 - <<'EOF'
import json, time, urllib.request
body = {"model": "MODEL", "stream": True, "max_tokens": 128, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False}, "tool_choice": "required",
        "messages": [{"role": "user", "content": "put on the new episode of severance"}],
        "tools": [{"type": "function", "function": {"name": "open_tile", "parameters": {"type": "object",
                  "properties": {"id": {"type": "string"}, "query": {"type": "string"}}, "required": ["id"]}}}]}
req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions", json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
t0 = time.perf_counter(); first = None
for line in urllib.request.urlopen(req):
    if line.startswith(b"data:") and first is None: first = time.perf_counter()
print(f"TTFT {1000*(first-t0):.0f} ms  total {1000*(time.perf_counter()-t0):.0f} ms")
EOF
```

---

## 3. Fallback plan (degradation ladder)

| Condition | Behaviour |
|---|---|
| ASR returns empty / VAD found no speech | Toast "Didn't catch that"; **never** send an empty/near-empty transcript to the LLM (whisper hallucinates on silence) |
| Fast path ambiguous (score 0.55–0.80 or near-tie) | Show top-3 candidate tiles; user picks with D-pad. No LLM call |
| Fast path no match | LLM tool call (Path B) |
| LLM server down / still warming after boot | Fast path only + "Assistant warming up/offline" chip. vLLM's first request after boot can take ~25 s (JIT) **[D, vLLM blog, 120B model]** → send a warm-up request at boot |
| Game in foreground | Fast path + system intents only (pause/quit/volume/screenshot/home). LLM requests ask "Pause game to use the assistant?" (user-configurable: allow with stutter) |
| LLM returns invalid/unknown tool or args | `tool_choice="required"` + JSON-schema-constrained args make malformed calls impossible in vLLM **[D]**; unknown ids → tool returns `isError:true` with a hint → one retry, then on-screen disambiguation |
| vLLM unavailable for a long time | Optional cold fallback: Ollama `qwen3.5:4b` or `phi4-mini` (both tagged `tools` **[D]**) |
| Mic missing | Hide voice UI; remote/gamepad always work |
| CDP/DOM path fails on a streaming site | Fall back to key events (arrows/Enter/Escape); then pixel CUA (optional); then hand back to user |
| MCP client misbehaves | Dual-era server handles both eras; destructive tools (power off, purchases, deleting) require on-screen confirmation |

---

## 4. Small LLMs with reliable tool calling (≤~35B total, MoE or ≤9B dense)

### 4.1 Official BFCL v4 numbers (leaderboard page "Last Updated: 2026-04-12"; data CSV fetched 2026-10-08) [D]
BFCL v4's **overall** score is dominated (~70% weight) by agentic web-search/memory/multi-turn categories **[R]**, which a
TV launcher does not need. The relevant columns for single-shot TV commands are **Non-Live AST** and **Live**.

| Model (mode) | Overall | Non-Live AST | Live | Multi-turn |
|---|---|---|---|---|
| Qwen3-4B-Instruct-2507 (FC) | 35.68 | 87.88 | 76.39 | 22.12 |
| Qwen3-30B-A3B-Instruct-2507 (FC) | 41.39 | 85.77 | 77.94 | 30.00 |
| Qwen3-8B (FC) | 42.57 | 87.58 | 80.53 | 41.75 |
| Qwen3-1.7B (FC) | 28.41 | 82.92 | 74.61 | 11.00 |
| Qwen3-0.6B (FC) | 23.93 | 71.79 | 56.62 | 3.62 |
| xLAM-2-8b-fc-r (FC) | 46.68 | 84.58 | 67.95 | 70.00 |
| ToolACE-2-8B (FC) | 42.44 | 87.10 | 77.42 | 38.38 |
| Hammer2.1-7b (FC) | 31.67 | 85.50 | 69.50 | 23.87 |
| Mistral-Small-2506 (Prompt) | 32.38 | 89.69 | 79.05 | 14.75 |
| Llama-4-Scout-17B-16E (FC) | 28.13 | 89.38 | 74.69 | 9.00 |
| Llama-3.2-3B-Instruct (FC) | 21.95 | 82.67 | 58.33 | 4.00 |
| Gemma-3-12b-it (Prompt) | 30.43 | 79.44 | 74.24 | 5.75 |
| Gemma-3-4b-it (Prompt) | 19.62 | 61.12 | 60.84 | 0.38 |
| Phi-4 (Prompt) | 28.79 | 69.56 | 60.70 | 3.88 |
| Granite-4.0-350m (FC) | 18.98 | 67.92 | 46.11 | 2.50 |

Not on the official board (as fetched): gpt-oss, LFM, Qwen3.5/3.6, Nemotron 3/3.5, Gemma 4.

### 4.2 Self-/vendor-reported BFCL v4 and other agent scores for 2026 models
| Model | Release | Arch / active | License | Tool-use scores | Source |
|---|---|---|---|---|---|
| Qwen3.5-9B | 2026-03-02 | dense 9B, vision | Apache-2.0 | BFCL-V4 **66.1**, TAU2 79.1 | model card **[D]** |
| Qwen3.5-4B | 2026-03-02 | dense 4B, vision | Apache-2.0 | BFCL-V4 **50.3**, TAU2 79.9 | model card **[D]** |
| Qwen3.5-35B-A3B | 2026-02-24 | MoE 3B active | Apache-2.0 | BFCL-V4 0.673 | llm-stats (self-reported) **[R]** |
| Qwen3.5-2B / 0.8B | 2026-03-02 | dense | Apache-2.0 | BFCL-V4 0.436 / 0.253 | llm-stats **[R]** |
| Qwen3.6-35B-A3B | 2026-04 | MoE 3B active | Apache-2.0 | TAU3 67.2, MCPMark 37.0, MCP-Atlas 62.8, Tool Decathlon 26.9 | model card **[D]** |
| Nemotron 3.5 Lightning 30B-A3B | 2026-08-11 | hybrid Mamba-2/MoE/attn, 3B active, MTP | OpenMDW-1.1 | PinchBench 85.37, IFBench 71.88, τ³-bench(Banking) 9.28; **no BFCL published** | model card **[D]** |
| Nemotron 3 Nano 30B-A3B | 2025-12 | hybrid MoE 3B active | NVIDIA open | BFCL v4 **53.76** (BF16) | tech report arXiv 2512.20848 **[D]** |
| gpt-oss-20b | 2025-08 | MoE ~3.6B active, MXFP4 | Apache-2.0 | τ-bench Retail 54.8 (high) / 47.3 (med) / 35.0 (low); no BFCL | model card arXiv 2508.10925 **[D]** |
| Gemma 4 E2B/E4B/12B/26B-A4B/31B | 2026-03-31…06-03 | E-models ~2.3B/4.5B effective; 26B MoE 3.8B active | Apache-2.0 | native function calling; τ²-bench figures conflict across sources (31B: 86.4 retail vs ~60 AA) | Google docs **[D]**, aggregators **[R]** |
| Granite 4.1 3B/8B/30B | 2026-04-29 | dense | Apache-2.0 | 8B τ²-bench 42.1 | aggregators **[R]** |
| Granite 4.2 3B/8B/30B | 2026-08-25 | dense, reasoning toggle | Apache-2.0 | BFCL-V4 52.4 / 50.3 / 61.4 (IBM table, relayed) | **[R]** |
| LFM2.5-2.6B | 2026-08-04 | dense, always reasons first | LFM Open License (<$10M revenue) | BFCL-V4 56.9, ToolSandbox 77.8 | vendor via write-ups **[R]** |
| LFM2.5-8B-A1B | 2026-05-28 | MoE 1B active | LFM Open License | BFCL-V4 48.5 | vendor **[R]** |
| FunctionGemma 270M | ~2025-12 | Gemma-3-based 270M | Gemma terms | 58% → 85% on Mobile Actions after task fine-tuning | Google **[D]** |
| Qwen3-4B-Instruct-2507 | 2025-07 | dense 4B, non-thinking | Apache-2.0 | see 4.1 | **[D]** |

Caveat: vendor BFCL-V4 numbers are self-reported and harnesses differ **[R]**. The right test is a SparkHome-specific eval
(§4.6).

### 4.3 Measured / reported speed on DGX Spark (GB10)
| Model | Engine / quant | Decode tok/s (c=1) | TTFT | Source |
|---|---|---|---|---|
| Nemotron 3.5 Lightning 30B-A3B | vLLM (user container) NVFP4 | **101.6** | — | **[M-user]** |
| Nemotron 3.5 Lightning 30B-A3B | vLLM 0.27.1 NVFP4, fp8 KV, marlin MoE | 81.27 bare; 111.36 MTP; 95.48 DFlash; **124.24 DSpark** (num_spec 3) | not measured | Classmethod 2026-08-12 **[R]** |
| Qwen3.6-35B-A3B | vLLM 0.25.0, `nvidia/Qwen3.6-35B-A3B-NVFP4` | 76.66 bare; 97.96 MTP2; **108.30 MTP3** | not measured | Classmethod 2026-07-13 **[R]** |
| Qwen3.6-35B-A3B | SGLang NVFP4 | 92.2 (peak) | — | recipe aggregator **[R]** |
| Qwen3-30B-A3B | Ollama Q4 / vLLM 4-bit | 85.2 / 77.1 | 161 ms / n.r. | NVIDIA forum ss121, 2026-08-10 **[R]** |
| Gemma 4 26B-A4B | vLLM NVFP4 (+MTP) / Ollama Q4_K_M (+MTP) | 30.3 bare, 54.9 MTP / 49.6 | **94 ms** / 511 ms | ss121 **[R]** |
| Nemotron 3 Nano 30B-A3B | llama.cpp UD-Q4_K_XL | 63.6 (pp 2,128) | — | NVIDIA forum **[R]** |
| gpt-oss-20b | Ollama 0.12.6 MXFP4 / llama.cpp | 58.3 (prefill 3,224) / ~74–80 | — | Ollama blog 2025-10 **[D]**, llama.cpp discussion **[R]** |
| GPT-OSS-120B | vLLM 4-bit / Ollama | 60.7 / 42.1 | 131 ms / 449 ms | ss121 **[R]** |
| Qwen3.5-9B | veloGB10 engine NVFP4 | 82.5 / 81.5 / 71.3 at 0/4K/8K ctx | ~1.0 / 2.5 / 4.3 s | HF card doth4580 **[R]** |
| Qwen3.6-27B (dense) | vLLM NVFP4 | 12.4 bare / 28.9 MTP | — | Classmethod **[R]** — dense 27B is too slow for voice |
| Qwen3-4B-Instruct-2507 (dense) | vLLM BF16 (if that is how it is served) | **~25 tok/s ceiling** | — | **[I]** 8 GB/token ÷ 208 GB/s; measure; FP8/4-bit would roughly double/quadruple |

Key inference **[I]**: decode on GB10 is memory-bandwidth-bound, so tok/s ≈ effective bandwidth ÷ bytes touched per token.
A 3B-active MoE at 4-bit touches ~1.5–2 GB/token (≈80–125 tok/s observed); a dense 4B at BF16 touches ~8 GB/token.

### 4.4 Ollama tags with the `tools` capability (fetched 2026-10-08) [D]
| Family | Tags shown | Badges |
|---|---|---|
| `qwen3.5` | 0.8b, 2b, 4b, 9b (latest), 27b, 35b, 122b | vision, tools, thinking |
| `qwen3.6` | 27b, 35b | vision, tools, thinking |
| `qwen3.8` | 27b | vision, tools, thinking |
| `qwen3` | 0.6b, 1.7b, 4b, 8b, 14b, 30b, 32b, 235b | tools, thinking |
| `nemotron-3.5-lightning` | 30b (25 GB), 30b-mlx | tools, thinking |
| `nemotron-3-nano` / `nemotron3` | 30b / 33b | tools, thinking (+vision for nemotron3) |
| `gpt-oss` | 20b (14 GB), 120b | tools, thinking |
| `gemma4` | e2b, e4b (latest), 12b, 26b, 31b | text+image input; page states native function calling |
| `granite4.1` | 3b, 8b, 30b | tools |
| `lfm2.5` / `lfm2.5-thinking` | 8b / 1.2b | tools, thinking |
| `phi4-mini` | 3.8b | tools (needs Ollama ≥0.5.13) |
| **`gemma3`** (the only model installed now) | 270m, 1b, 4b, 12b, 27b | **vision only — no `tools` badge** → useless for tool calling |

Known 2026 issues: Qwen3.5 tool calling had parser/chat-template bugs in Ollama/vLLM in Feb–Apr 2026 (fixes landed; re-pull
GGUFs) **[R]**. An Aug-2026 Spark benchmark found "tool-calling failures were often server configuration, fixed by matching
parser flags" **[R]**.

### 4.5 Recommended serving configuration (vLLM)
```bash
# Nemotron 3.5 Lightning NVFP4 — flags from the NVIDIA model card's Spark recipe [D], with memory cut for coexistence [I]
vllm serve nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 \
  --moe-backend marlin --kv-cache-dtype fp8 --enable-prefix-caching \
  --mamba-backend flashinfer --mamba-cache-mode align \
  --speculative_config.model nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark \
  --speculative_config.num_speculative_tokens 3 \
  --reasoning-parser nemotron_v3 --tool-call-parser qwen3_coder --enable-auto-tool-choice \
  --max-model-len 16384 --max-num-seqs 2 \
  --gpu-memory-utilization 0.30        # recipe uses 0.85; start ~0.30 and raise until the KV pool fits 16k ctx
# Qwen3.6-35B-A3B NVFP4 (A/B candidate) — Classmethod's Spark command [R], same memory edits:
#   --quantization modelopt --kv-cache-dtype fp8 --max-num-batched-tokens 4096 (needed for Mamba cache block size)
#   --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
#   --speculative-config '{"method":"mtp","num_speculative_tokens":3}' --enable-prefix-caching
# Qwen3-4B-Instruct-2507: --enable-auto-tool-choice --tool-call-parser hermes   (Qwen/vLLM docs [D])
# gpt-oss-20b:            --tool-call-parser openai                             (vLLM docs [D])
```
Request-side (OpenAI-compatible):
- `"chat_template_kwargs": {"enable_thinking": false}` (Qwen3.5/3.6, Nemotron 3.5) **[D]**.
- `"tool_choice": "required"` (vLLM ≥0.8.3) — uses structured outputs; *"guaranteed to generate one or more tool calls …
  output format strictly follows the schema"* **[D]**. Include a `reply_to_user(text)` and `ask_clarification(options[])`
  tool so "no action" is still a valid tool call. First use of a schema compiles a grammar (several seconds) → warm up at boot **[D]**.
- Optional server floor: `--tool-strict-level parameter` pins argument schemas for every tool (documented in vLLM main docs,
  Oct 2026; verify your image has it) **[D]**.
- Prompt layout for prefix-cache hits: [system + tool schemas (static, deterministic order)] → [few-shot examples] →
  [dynamic UI state + transcript last]. Never put the full catalog in the prompt: resolve entities first (fast path) and
  inject the top-5 candidates, or give the model a `search_catalog(query)` tool **[I]**.
- Sampling for tool calls: temperature 0–0.2 **[I]** (Qwen's recommended non-thinking defaults are for chat **[D]**).

### 4.6 Evaluation gate (must exist before choosing the model)
Build a 150–300 utterance set from real phrasing ("put on Severance", "open the game with the cowboy", "turn it down",
"what's on Plex", noisy ASR variants). Score: valid-call rate, exact action+args accuracy, p50/p95 latency, under
(a) idle, (b) game running. Pick the model by this, not by BFCL.

---

## 5. ASR on arm64 + CUDA

| Model | Size | Langs | Open ASR avg WER | Speed | Runtime on Spark | License | Notes |
|---|---|---|---|---|---|---|---|
| Whisper large-v3-turbo (installed) | 809M (4-layer decoder) | 99 | ~7.6–7.8 | RTFx 200–350 (batched, conflicting) | whisper.cpp CUDA **[M-user works]** | MIT | Pads to 30 s window unless `-ac`; hallucinates on silence (use VAD, `-nth`, `-sns`) |
| **Parakeet-TDT-0.6B-v2** | 600M | English | **6.05** | RTFx ~3,386 (A100 batched) | onnx-asr (CPU; GPU needs ORT CUDA build), NeMo (PyTorch/NGC container), transcribe.cpp GGUF | CC-BY-4.0 on HF (reported); NIM uses NVIDIA license | Best accuracy/speed for English commands **[R]**; GPU-PB word boosting supports TDT/RNNT **[D]** |
| Parakeet-TDT-0.6B-v3 | 600M | 25 EU | 6.32–6.34 | RTFx ~3,333 | same | same | Use if non-English needed |
| Canary-1B-flash | ~1B | en/de/fr/es + translation | 6.35 | RTFx ~1,045 | NeMo, transcribe.cpp GGUF | CC-BY-4.0 (reported) | No advantage for English-only commands |
| **Nemotron Speech Streaming EN 0.6B** | 600M cache-aware FastConformer-RNNT | English, punctuated | 6.93 (1.12 s chunk) / 7.07 (0.56 s) / 7.67 (0.16 s) / 8.43 (0.08 s) | streaming | NeMo 25.11+, Transformers ≥5.13 (streaming API), **NeMo-Speech.cpp (GGUF, ggml, CUDA)**; card lists **DGX Spark among test hardware** | NVIDIA Open Model License | Released 2026-03-12/13 **[D]**; partial transcripts while the button is held |
| Nemotron 3.5 ASR Streaming 0.6B | 600M | ~40 locales | — | streaming | NeMo / GGUF ports | OpenMDW-1.1 or NVIDIA (conflicting) | Multilingual variant (2026-06) **[R]** |

Notes:
- **Latency for 2–5 s commands** is dominated by fixed overheads, not RTFx. With PTT there is no VAD endpoint wait.
  Parakeet's encoder sees only ~37 frames for 3 s (80 ms frames) → tens of ms on GPU **[I]**; whisper-turbo's 32-layer
  encoder runs over a full 30 s window by default (`-ac 0`) → hundreds of ms **[I]**; `-ac ~150–256` shortens it at an accuracy
  cost **[D/R]**. No published GB10 numbers exist for any of these **[unknown]** → measure (§2).
- **CPU option avoids GPU contention with games.** onnx-asr: Parakeet v2/v3 RTFx 36 on a Ryzen 9800X3D, 1.0 on a Cortex-A53
  **[R]**; X925 cores are much faster than A53 (expect somewhere between) **[I]**. Install: `pip install "onnx-asr[cpu,hub]"`
  (CPU ORT wheels exist for aarch64; ORT **CUDA** wheels for aarch64/CUDA 13 were nightly/self-build as of Apr–Jul 2026 **[R]**;
  avoid INT8 ONNX on the GB10 GPU, use FP32 exports **[R]**).
- **GPU option:** NeMo/PyTorch on Spark works via NGC containers or PyTorch ≥2.11 ARM64 CUDA wheels (sm_121 coverage
  disputed; test with a real kernel) **[R]**. User has the docker group → prefer an NGC container.
- **Biasing toward tile/game names:** whisper `--prompt "Netflix, Plex, Cyberpunk 2077, …"` **[D]**; NeMo **GPU-PB** phrase
  boosting at decode time for CTC/RNNT/TDT/Canary (key phrases must be cased like the model output) **[D]**; whisper.cpp
  `whisper-command -cmd commands.txt` "guided mode" classifies audio into a fixed command list **[D]** (prior art).
- **Silero VAD:** v6.2 (latest tag v6.2.1); 512-sample windows at 16 kHz (32 ms); <1 ms per chunk on one CPU thread
  **[D/R]**; ggml model `ggml-silero-v6.2.0.bin` is built into whisper.cpp (`--vad`, `-vt`, `-vspd`, `-vsd`, `-vp`) **[D]**.
- **Audio-native LLMs** (Gemma 4 E2B/E4B/12B accept audio **[D]**; Nemotron 3 Nano Omni has a Parakeet encoder **[R]**)
  could map audio → tool call in one model, but lose the visible transcript and the deterministic fast path → not v1 **[I]**.

---

## 6. Wake word on Linux arm64 (phase 2)

| Engine | Status (Oct 2026) | arm64 / Python 3.12 | Custom wake word | License |
|---|---|---|---|---|
| **Picovoice Porcupine** | Free-tier AccessKeys **disabled after 2026-06-30**; "no non-commercial tier planned"; SDK won't run without a valid key (Picovoice email quoted in HA forum, 2026-06-04) **[R]** | Linux arm64 listed only for some boards **[R]** | Console training | Commercial ($500/mo+ developer tier **[R]**) → **avoid** |
| **openWakeWord** | PyPI 0.6.0 (Feb 2024) still latest; commits through Dec 2025 **[R]** | `tflite-runtime` aarch64 wheels stop at Python 3.11 → **use the ONNX runtime path** on 3.12 **[R]** | Training notebook is fragile (pinned TF 2.8 / torch 1.13; Colab fixes exist; ~1–2 h) **[R]** | Code Apache-2.0; **pre-trained models CC BY-NC-SA** **[R]** |
| **microWakeWord** | Runtime `pymicro-wakeword` 2.4.1 packaged in Debian (2026-07); training deps drifting ("Is microWakeWord dead?" thread, 2026-07) **[R]** | depends on TFLite 2.14 lib; Linux Voice Assistant runs OWW+MWW on aarch64 Docker **[D]** | microWakeWord training repo | Apache-2.0 |
| Linux Voice Assistant (OHF-Voice) | Experimental HA satellite (ESPHome protocol), amd64+aarch64, Python ≥3.11 **[D]** | yes | via OWW/MWW models | Apache-2.0 |

**The real problem is the TV, not the detector.** A far-field mic near the TV hears the program audio. Because every sound the TV
plays originates on the Spark, PipeWire's `libpipewire-module-echo-cancel` (WebRTC AEC; `monitor.mode = true` captures the
reference from the default sink's monitor) can cancel it **[D]**; residual performance with TV DSP delay is unknown **[unknown]**.
Also duck/pause media the instant a wake word or PTT fires (trivial since the Spark owns playback) **[I]**.
Recommendation: ship PTT; add wake word only after AEC is measured (false accepts/hour with a movie playing, misses at 3 m).

---

## 7. TTS (not on the critical path)

| Engine | Size / license | Latency evidence | Notes |
|---|---|---|---|
| **Piper** (installed, en_US-lessac-medium) | ~60M VITS; new home **OHF-Voice/piper1-gpl** (GPL-3.0), legacy repo read-only since Oct 2025; v1.7.0 (Aug 15) / v1.8.0 (Sep 4, 2026, reported) | ~30 ms TTFB on an M2 CPU (40 chars) **[R]**; 150–250 ms per 10-word sentence **[R]** | CPU-only, no GPU contention — keep for confirmations |
| **Kokoro-82M** | 82M, Apache-2.0 | ~300 ms first audio on GPU (chunk 400), ~45 ms on RTX 5090; seconds on old CPUs **[R]** | Nicer voice; GPU use during games is a contention cost |
| Kyutai Pocket TTS | ~100M, MIT | ~200 ms first audio on laptop CPU (reported secondhand) **[R]** | CPU voice cloning |
| Chatterbox Turbo | 350M, MIT, watermarked | ~75 ms (vendor, GPU) **[R]** | Overkill for confirmations |
| Supertonic 3 / Kitten TTS 0.8 | 99M OpenRAIL-M / 14–80M Apache-2.0 | CPU real-time-ish **[R]** | Alternatives if Piper voice is disliked |

Policy **[I]**: speak only short confirmations or answers to questions; never during gameplay or while video plays unless the
user asked; route through the same PipeWire sink so AEC references it.

---

## 8. Computer-use / GUI-agent models runnable locally (2026)

| Model | Sizes | License | Base | Key scores | Runtime notes |
|---|---|---|---|---|---|
| **Holo3.1** (H Company, 2026-06-01/02) | 0.8B, 4B, 9B, 35B-A3B (+FP8, **NVFP4**, GGUF for 35B) | Apache-2.0 (4B card **[D]**) | Qwen3.5 family | AndroidWorld 71–72% (4B/9B), 79.3% (35B-A3B) **[D]** | **On DGX Spark (35B-A3B):** BF16 326 / FP8 404 / NVFP4 568 total tok/s (12k in, 1k out, 3×1080p images, c=1); **avg step 6.8 s FP8 → 3.3 s NVFP4+NVIDIA harness opts**; native function calling **[D]** |
| Holo3-35B-A3B (2026-03) | 35B-A3B | Apache-2.0 | Qwen3.5-35B-A3B | OSWorld-Verified 77.8% **[D]** | vLLM/SGLang |
| Holo4 (2026-09-28) | 27B dense, 35B-A3B | 27B **CC BY-NC 4.0**; 35B-A3B Apache-2.0 **[R]** | Qwen3.8 / Qwen3.5 | 27B: 85.2% OSWorld (H harness) **[R]** | newest; dense 27B decodes slowly on Spark |
| Holo2 (2025-11) | 4B, 8B, 30B-A3B | 30B: CC-BY-NC (listing) **[R]** | Qwen3-VL | ScreenSpot-Pro 57.2/58.9/66.1; OSWorld-G 69.4/70.1/76.1 **[D]** | superseded |
| **Fara1.5** (Microsoft; card dated 2026-05-21) | 4B, 9B, 27B | MIT | Qwen3.5 | WebVoyager 80.8/86.6/89.3; Online-Mind2Web 57.3/63.4/72.3; WebTailBench 27.4/32.3/40.2 **[D]** | **browser-only, vision-only**; `<tool_call>` `computer_use` actions (click/type/key/scroll/visit_url/…); vLLM ≥0.19.1; pauses at critical points **[D]** |
| Fara-7B (2025-11) | 7B | MIT | Qwen2.5-VL-7B | WebVoyager 73.5, Online-M2W 34.1 **[D]** | superseded by Fara1.5 |
| **Qwen3.5-9B / 4B** (native) | 9B / 4B | Apache-2.0 | — | ScreenSpot-Pro 65.2 / 60.3; OSWorld-Verified 41.8 / 35.6; AndroidWorld 57.8 / 58.6 **[D]** | One model for tools + vision; Ollama `qwen3.5:4b/9b` (tools, vision) |
| Qwen3-VL-30B-A3B | 30B-A3B | Apache-2.0 | — | ScreenSpot-Pro 60.5, OSWorld-Verified 30.6, AndroidWorld 55.0 (per Qwen3.5 card) **[D]** | superseded by Qwen3.5 |
| UI-TARS-1.5-7B | 7B | Apache-2.0 | Qwen2.5-VL | OSWorld 27.5 (README); **TVWorld-N 1.6%** **[R]** | UI-TARS-2 (2025-09 report): **open weights not confirmed** **[R]** |
| OpenCUA-7B / 32B / 72B | 7/32/72B | MIT | Qwen2.5-VL | OSWorld-Verified 27.9 / 34.8 / 45.0; ScreenSpot-Pro (7B) 50.0 **[D]** | vLLM ≥0.12.0 `--trust-remote-code` **[D]** |
| GLM-4.6V-Flash | 9B | MIT | — | no GUI benchmarks found; GLM-4.6V-106B OSWorld 37.2 (third-party) **[R]** | — |
| Qwen-UI-Agent (arXiv 2607.28227) | not stated | not stated | — | OSWorld-Verified 79.5, ScreenSpot-Pro 81.5 **[R]** | weights status unknown |
| Qwen3.8-27B | 27B dense | Apache-2.0 | — | OSWorld-Verified 84.3% (BenchLM, self-reported) **[R]** | too slow on Spark for interactive use (~12–29 tok/s for 27B dense **[R]**) |

**TV / D-pad UIs — TVWorld** (arXiv 2601.13142; HKU, TCL, HKBU; ACL Findings 2026) **[R]**: Google-TV-derived screens; action
space `UP, DOWN, LEFT, RIGHT, OK, EXIT, HOME, SETTING, FINISH`. TVWorld-N (navigation) success: TVTheseus (Qwen3-VL-8B +
SFT/RL) **68.3**, Gemini 3 Flash 66.4, GPT-5 mini 60.2, Qwen3-VL-32B 39.0, Claude Haiku 4.5 25.4, Qwen3-VL-8B 20.0,
GUI-Owl-32B 15.4, GUI-Owl-7B 5.4, OpenCUA-7B 5.0, UI-TARS-1.5-7B 1.6. TVWorld-G (focus grounding Acc@0.5): TVTheseus 81.8,
Qwen3-VL-8B 78.1. Failure cause per the paper: no "topology awareness" (tracking focus, planning key sequences); pointer-trained
models degrade sharply. Code: github.com/Lqf-HFNJU/TVTheseus (weights release not stated).
**Design consequence [I]:** expose focus explicitly (`focused_id`, neighbours per direction) and offer `select(tile_id)` so an agent
never has to plan D-pad paths on our own UI.

---

## 9. Harness design

### 9.1 Semantic tools vs accessibility trees vs pixels
| Approach | Use for | Cost/step | Reliability |
|---|---|---|---|
| **Semantic tools** (our API) | Everything inside SparkHome | 1 LLM call ≈0.4–0.8 s; or 0 with fast path | Highest; deterministic; testable |
| **DOM / accessibility tree via CDP** | Chrome-hosted streaming apps (Netflix/YouTube/etc. in kiosk windows) | 1–2.5 s/step **[I]** | Good; DOM changes break selectors less than pixels break grounding |
| **Pixel CUA** (screenshot → click/key) | Unknown native UIs, games' launchers, canvases | 3.3–6.8 s/step on Spark **[D]** | Lowest; poor on D-pad UIs **[R]** |

Suggested SparkHome tool set (names stable, deterministic order, small schemas, enums where possible) **[I]**:
`get_ui_state()` → `{screen, focused_id, rows:[{id,title,tiles:[{id,title,kind,neighbors:{up,down,left,right}}]}], now_playing}` ·
`search_catalog(query, kind?)` · `open_tile(id)` · `select(id)` · `navigate(direction, steps=1)` · `back()` · `home()` ·
`media(action: play|pause|stop|seek, seconds?)` · `volume(action: up|down|set|mute, value?)` · `launch_game(appid)`
(→ `steam.pipe` `-applaunch APPID`, measured working by user) · `search_in_app(app, query)` · `reply_to_user(text)` ·
`ask_clarification(options[])`. Mark destructive ones (power, purchases, delete) with `destructiveHint` and require on-screen confirm.

Input injection on Xorg without the `input` group **[I]**: XTEST via `python-xlib` (or `xdotool`, not installed) works for
any X client on `DISPLAY=:1`; `/dev/uinput` needs root/udev rules. Screenshots: `python-mss`/XShm or `ffmpeg -f x11grab`.
For Chrome pages prefer CDP `Input.dispatchKeyEvent` over XTEST.

### 9.2 Minimal MCP server over Streamable HTTP — exact protocol (2026)

**Current spec revision: `2026-07-28`** (final 2026-07-28; "largest revision in the protocol's history") **[D]**. It is
**stateless**: no `initialize`, no `notifications/initialized`, no `Mcp-Session-Id`, no standalone GET stream, no
`Last-Event-ID` resumption. Earlier revisions (`2025-11-25`, `2025-06-18`, `2025-03-26`) are "legacy" and use the handshake.
A server may be **dual-era** on one endpoint **[D]**.

**Modern requests (2026-07-28)** — every POST **MUST** carry **[D]**:
- headers: `Content-Type: application/json`; `Accept: application/json, text/event-stream`; `MCP-Protocol-Version: 2026-07-28`;
  `Mcp-Method: <method>` (all requests); `Mcp-Name: <params.name|params.uri>` for `tools/call`, `resources/read`, `prompts/get`
  (non-ASCII → `=?base64?…?=`).
- body `params._meta`: `io.modelcontextprotocol/protocolVersion` (required), `io.modelcontextprotocol/clientCapabilities`
  (required), `io.modelcontextprotocol/clientInfo` (optional, SHOULD).
- results **MUST** include `"resultType": "complete"`; servers SHOULD add `_meta["io.modelcontextprotocol/serverInfo"]`.
- **Cacheable results (`server/discover`, `tools/list`, other lists) need `ttlMs` and `cacheScope`** — the official Python SDK
  2.3.0 client **rejects** a `tools/list` result without them **[M]** (the spec pages show them in examples; the SDK schema marks
  them required on the 2026-07-28 wire).

Minimal method set: `server/discover` (**MUST** implement), `tools/list`, `tools/call`.
```json
// POST /mcp   headers: MCP-Protocol-Version: 2026-07-28, Mcp-Method: server/discover
{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{
  "io.modelcontextprotocol/protocolVersion":"2026-07-28",
  "io.modelcontextprotocol/clientInfo":{"name":"x","version":"1"},
  "io.modelcontextprotocol/clientCapabilities":{}}}}
// 200 application/json
{"jsonrpc":"2.0","id":1,"result":{"resultType":"complete","supportedVersions":["2026-07-28","2025-11-25"],
  "capabilities":{"tools":{}},"instructions":"…","ttlMs":0,"cacheScope":"private",
  "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"sparkhome","version":"0.1.0"}}}}

// tools/call   headers: Mcp-Method: tools/call, Mcp-Name: open_tile
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"open_tile","arguments":{"id":"netflix"},"_meta":{…}}}
{"jsonrpc":"2.0","id":3,"result":{"resultType":"complete","content":[{"type":"text","text":"Opening Netflix"}],"isError":false}}
```
Status codes / errors **[D]**: notification → `202` no body · request → `200` with `application/json` **or** `text/event-stream`
(client must accept both; plain JSON is enough for a minimal server) · header/body mismatch or missing required header →
`400` + `-32020 HeaderMismatch` · unsupported version → `400` + `-32022` with `data:{supported:[…],requested}` · missing
required `_meta` → `400` + `-32602` · missing client capability → `400` + `-32021` · unknown method → `404` + `-32601` ·
bad `Origin` → `403` (DNS-rebinding guard, **MUST** validate) · GET/DELETE on a modern-only server → `405` · tool runtime
failures → `200` with `isError:true` (model can self-correct). Bind to `127.0.0.1` when local (**SHOULD**).

**Legacy requests (2025-11-25 and earlier)** **[D]**: `initialize` (`params.protocolVersion`, `capabilities`, `clientInfo`) →
result `{protocolVersion, capabilities, serverInfo, instructions?}`; then client sends notification
`{"jsonrpc":"2.0","method":"notifications/initialized"}` (→ `202`); then `tools/list` / `tools/call`; HTTP clients send
`MCP-Protocol-Version: <negotiated>` on subsequent requests (since 2025-06-18); `Mcp-Session-Id` is optional (server MAY
assign) — a stateless legacy server simply never mints one. Version negotiation: server echoes the client's version if
supported, else its latest. Dual-era detection by clients: send a modern request; a `400` whose body is not a recognized
modern error → fall back to `initialize` **[D]**.

**Tested reference** (stdlib, ~170 lines incl. self-test): `scratchpad/research/mcp_min.py`
```
python3 mcp_min.py --selftest    # "selftest OK" [M]
# Interop [M] (both official SDK clients, v2):
#  Python mcp 2.3.0:                 auto -> 2026-07-28 | pin 2026-07-28 -> 2026-07-28 | legacy -> 2025-11-25
#  TS @modelcontextprotocol/client 2.3.1: auto -> 2026-07-28 | {pin} -> 2026-07-28 | legacy -> 2025-11-25
# (tests: scratchpad/research/tests/interop_sdk.py, tests/ts-client/interop.mjs)
```
If the SparkHome backend is Node, prefer the official **TypeScript SDK v2** (`@modelcontextprotocol/server` +
`@modelcontextprotocol/express`; `createMcpExpressApp()` returns an Express app with DNS-rebinding/Host checks; factory-based
handler; 2025-era clients are served statelessly by default; GET/DELETE → 405) **[R/D]**. Python: `mcp` ≥2 (`MCPServer`,
formerly FastMCP) supports 2026-07-28 **[R]**; pin `mcp>=2,<3`.

**Claude Code** **[D]**:
```bash
# On the Spark (server bound to 127.0.0.1): from the Mac, tunnel then add
ssh -N -L 8765:127.0.0.1:8765 spark@<spark-host> &
claude mcp add --transport http sparkhome http://127.0.0.1:8765/mcp            # -t http, scope local (default)
claude mcp add --transport http sparkhome --scope project http://127.0.0.1:8765/mcp   # writes .mcp.json {"type":"http","url":…}
claude mcp add --transport http sparkhome http://spark.lan:8765/mcp --header "Authorization: Bearer $TOKEN"  # if LAN-bound (then add auth!)
claude mcp list ; claude mcp get sparkhome ; /mcp (inside a session)
# Runtime knobs: MCP_SDK_GENERATION=v1|v2 ; MCP_PROTOCOL_NEGOTIATION=auto|legacy ; MCP_TIMEOUT=10000 ;
# per-server "timeout" ms in .mcp.json; HTTP tool idle timeout 5 min (CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT)
```
Claude Code's v2 runtime (TS SDK 2.0) asks HTTP/stdio servers whether they support 2026-07-28 and uses it when they do **[D]**.

### 9.3 Browser DOM agents over CDP
- Chrome ≥136 ignores `--remote-debugging-port/pipe` for the **default** profile; use a dedicated `--user-data-dir` (and sign
  in to the streaming services once in that profile) **[D]**. CDP = full control of the logged-in sessions: confirm with
  `ss -ltnp | grep 9222` that it listens on 127.0.0.1 only and never expose or forward it to the LAN **[I]**.
- **Playwright MCP** (microsoft/playwright-mcp): accessibility-tree snapshots (no vision model needed), `--cdp-endpoint` to
  attach to the running Chrome, `--port` for HTTP at `/mcp`, `--caps vision` for coordinate tools; tools include
  `browser_snapshot`, `browser_click`, `browser_press_key`, `browser_type`, `browser_navigate`, `browser_evaluate` **[D]**.
  Its Docker image is headless-Chromium-only **[D]**, so run it natively on the Spark.
- **browser-use** (v0.13.x reported mid-2026): DOM-first, Ollama supported, CDP attach; 8B-class local models handle simple
  lookups but struggle on multi-step tasks, ~30B tier recommended (third-party guide) **[R]**. Ollama's 4,096-token default
  context is too small for browser agents — raise `OLLAMA_CONTEXT_LENGTH` **[D/R]**.
- TV-style web UIs that support arrow-key focus navigation reduce the problem to `press_key` + `document.activeElement`
  inspection — the TVWorld lesson in DOM form **[I]**.

### 9.4 Deterministic fast path before the LLM
- Prior art: **Home Assistant Assist** "Prefer handling commands locally": built-in **hassil** template matcher first, LLM
  only for what it can't parse **[D]**; one blog measured 0.085 s local intent vs 4.9 s large LLM **[R]**; hassil is strict,
  so community add-ons (hass-closest-intent, Assist Canonicalizer) fuzzy-map garbled STT first **[R]**. Caveat: locally
  matched commands bypass the LLM's safety logic (e.g. PIN for locks) — keep risky actions out of the fast path **[R]**.
- Reference implementation `scratchpad/research/fastpath.py` **[M]**: regex templates (home/back/media/volume/nav/search/open)
  + fuzzy title resolver (accept ≥0.80 with a 0.05 margin over #2; 0.55–0.80 → on-screen top-3 disambiguation; else LLM).
  Self-check passes ("play red dead redemption two" → Red Dead Redemption 2, "put on you tube" → YouTube, bare "up" → nav);
  **~3.0 ms/query over 513 titles** on a Mac (stdlib difflib). Upgrade: rapidfuzz; phonetic keys only if logs show misses.
- Add per-tile **aliases** (e.g. "RDR2", "the cowboy game") learned from LLM-resolved commands (log → confirm → alias) **[I]**.

### 9.5 Home Assistant / Wyoming as prior art and integration
- **Wyoming** (OHF-Voice): one JSON header line (`type`, `data`, `data_length`, `payload_length`) + optional binary payload;
  events `transcribe`/`audio-start`/`audio-chunk`/`audio-stop`/`transcript`, `synthesize`, `detect`/`detection`,
  `describe`/`info`; servers exist for faster-whisper, whisper-cpp, vosk, piper, openwakeword, microwakeword; **no auth or
  encryption** (trusted networks only) **[D]**. Useful as off-the-shelf building blocks bound to 127.0.0.1.
- If the user runs Home Assistant, forward "home" intents to `POST /api/conversation/process` (`text`, `language`,
  `conversation_id`; `Authorization: Bearer <long-lived token>`) **[D/R]** rather than re-implementing device control.

---

## 10. Contention: ASR + LLM vs games on one GB10

Facts:
- One 128 GB LPDDR5x pool (273 GB/s spec; user measured ~208 GB/s) shared by CPU, GPU, OS, games and models **[D/M-user]**.
- vLLM `--gpu-memory-utilization` is a fraction of the **unified** pool: 0.9 ≈ 115 GB reserved at start **[R]**; a forum guide
  went from ~117 GB (default) to ~32 GB (tuned, 0.2 floor for a 19 GB model) **[R]**. NVIDIA's own Spark recipes use 0.85–0.91
  **[D/R]** — fine for a dedicated inference box, wrong for a gaming TV box **[I]**. nvidia-smi does not show GB10 memory;
  read `MemAvailable` in `/proc/meminfo` **[R]**. cgroup memory caps reportedly don't constrain CUDA unified allocations **[R]**.
- During inference one poster saw GPU read bandwidth fall from ~161 to ~90 GB/s **[R]** (method unknown). No published
  frame-time study of gaming + LLM on a Spark exists **[unknown]**.
- Arithmetic **[I]**: a 40-token tool call on a 3B-active NVFP4 model moves ~60–80 GB through memory in ~0.4 s, i.e. it
  saturates bandwidth for that window; Parakeet/whisper bursts are tens to hundreds of ms; an idle resident model costs
  capacity but ~no bandwidth.

Mitigations (in order of simplicity):
1. **Policy gating** while a game is foreground: fast path + system intents only; LLM on explicit confirmation (or pause the
   game first) **[I]**.
2. **Keep one LLM resident and warm** (reloading 20 GB also floods bandwidth and takes long; first request after boot ~25 s JIT
   in vLLM's own Spark test **[D]**); size its KV pool small (`--gpu-memory-utilization ~0.3`, `--max-model-len 16384`,
   `--max-num-seqs 2`; vLLM recommends ≤4 seqs on Spark **[D]**). Don't run two big vLLM containers at once.
3. **Ollama** controls **[D]**: `keep_alive` per request (`0` = unload after reply, negative = forever), `OLLAMA_KEEP_ALIVE`
   (default 5 min), unload now: `curl :11434/api/generate -d '{"model":"M","keep_alive":0}'` or `ollama stop M`; inspect
   `/api/ps` (`expires_at`, `size_vram`); `OLLAMA_MAX_LOADED_MODELS` default 3×GPUs; `OLLAMA_NUM_PARALLEL` default 1;
   `OLLAMA_CONTEXT_LENGTH` default 4096.
4. **vLLM sleep mode** **[D]**: `VLLM_SERVER_DEV_MODE=1` + `--enable-sleep-mode`; `POST /sleep?level=1` (weights → CPU RAM,
   KV discarded), `level=2` (discard both; wake needs `reload_weights`), `POST /wake_up`. On unified memory, level 1 frees only
   the KV pool (weights "offloaded" to the same DRAM) **[I]** — a small KV pool from step 2 makes sleep mostly unnecessary.
5. **CPU pinning**: games run x86 code under FEX (CPU-heavy). Put ASR-on-CPU, VAD, wake word, TTS, the MCP server on the
   **A725** cores, games on **X925**. Reported layout: A725 = CPUs 0–4 and 10–14, X925 = 5–9 and 15–19 (StorageReview) **[R]** —
   verify: `for c in $(seq 0 19); do echo cpu$c $(cat /sys/devices/system/cpu/cpu$c/topology/cluster_id) $(cat /sys/devices/system/cpu/cpu$c/cpufreq/cpuinfo_max_freq); done`;
   systemd units: `CPUAffinity=0-4 10-14`, `Nice=10` **[I]**.
6. **GPU priority**: no supported cross-process priority knob between a Vulkan game and CUDA inference was found for GB10 (MIG
   support on GB10 not confirmed) **[unknown]** → rely on gating, not scheduling.
7. Measure: frame-time p99 with `DXVK_HUD=frametimes` (Proton) while firing 20 LLM tool calls vs. none; log `MemAvailable`.

---

## 11. Voice-input hardware notes (from the voice-stack angle)
- The **original IR "Apple Remote"** (the remote of the Gen-1 Apple TV) has **no microphone** → voice must come from another
  device **[I/general knowledge]**.
- **Siri Remote mic on Linux**: `azais-corentin/siri-remote` (GPL-3.0) republishes the **3rd-gen** Siri Remote mic as a PipeWire
  source (Opus CELT, 48 kHz, 20 ms frames, HID report 0xFA while the Siri button is held); **gen-1/gen-2 out of scope** **[D]**.
  `zanneth/siriremote` (Rust) also records it but sends audio to OpenAI whisper-1 **[D]**. Both require disabling BlueZ's
  **`hog` plugin**, which breaks other BLE HID-over-GATT devices **[D]** — the user's **Xbox Series controller pairs over BLE**,
  so this conflicts **[I]**.
- Xbox controllers have no usable BT mic; generic "voice air-mouse" remotes (2.4 GHz dongle) *may* expose a USB-audio mic —
  unverified for Linux; test with `arecord -l` / `pactl list short sources` and `evtest` for the voice key **[R/I]**.
- Cheapest robust option: a **phone PWA** with a hold-to-talk button streaming 16 kHz PCM/Opus over WebSocket to the Spark;
  `getUserMedia` requires a secure context (HTTPS, e.g. mkcert/Tailscale certs) **[I]**.

---

## 12. Incremental delivery plan (each step has an evidence gate)

| # | Outcome | Evidence gate |
|---|---|---|
| 0 | Baselines on the Spark: whisper-server latency on 20 recorded commands; vLLM TTFT/total for one tool call; `MemAvailable` with model resident; CPU cluster map | Numbers recorded in the repo (commands in §2) |
| 1 | PTT capture (pre-roll ring buffer + 100 ms tail) → whisper-server → `fastpath.py`-style matcher → launcher action API; transcript shown on screen | p50 ≤ 400 ms button-up→action on 50 commands; ≥90% correct on scripted set |
| 2 | LLM fallback: vLLM Nemotron 3.5 Lightning, thinking off, `tool_choice="required"`, prefix-cached prompt, `reply_to_user`/`ask_clarification` tools; eval set (§4.6); A/B Qwen3.6-35B-A3B NVFP4 | ≥98% valid calls, ≥90% correct action, p50 ≤ 1.0 s |
| 3 | MCP endpoint (dual-era; port `mcp_min.py` or use SDK v2) exposing the same tools; Claude Code via SSH tunnel | `claude mcp list` shows Connected; scripted tool calls pass in both eras |
| 4 | Contention policy + measurement with one heavy game (Cyberpunk) | frame-time p99 delta documented; policy defaults chosen; vLLM memory fraction tuned with `MemAvailable` ≥ game needs |
| 5 | ASR upgrade only if gate 1 fails: Parakeet-TDT-0.6B-v2 (onnx-asr CPU, A725-pinned) or NeMo GPU + GPU-PB with tile names; optional Nemotron streaming captions | WER on own command set and p95 latency beat step-1 numbers |
| 6 | Chrome streaming apps under CDP (dedicated profile) with DOM/AX tools + key events | 10 scripted multi-step tasks ≥80% success, ≤3 s/step |
| 7 | Optional wake word: openWakeWord/microWakeWord + PipeWire echo-cancel (`monitor.mode`) | <1 false accept/hour with a movie playing; ≥90% hits at 3 m |
| 8 | Optional pixel CUA for unknown UIs (Holo3.1-4B/9B or Qwen3.5-9B; Holo3.1-35B-A3B NVFP4 if quality needed) | only as background tasks; per-step latency measured |

---

## 13. Unknowns / open questions
- No GB10 measurements found for whisper.cpp large-v3-turbo, Parakeet, Canary or Nemotron streaming latency on 2–5 s clips.
- How the user's Qwen3-4B-Instruct-2507 container is quantized (BF16 would cap decode near ~25 tok/s [I]).
- Exact `--gpu-memory-utilization` that leaves enough RAM for the heaviest games while keeping a 16k KV pool.
- Real frame-time impact of a 0.4 s decode burst during gameplay on GB10 (no published data).
- Whether a cross-process GPU priority/MIG mechanism exists on GB10.
- Which mic hardware the user will accept (USB mic, phone PWA, voice remote) — determines whether wake word/AEC is ever needed.
- Whether any 2.4 GHz "voice remote" exposes a standard USB-audio mic on Linux (needs a hands-on test).
- Nemotron 3.5 Lightning has no published BFCL/function-calling score; its tool-call quality on SparkHome's tools is unmeasured.
- Holo4 / Fara1.5 / Holo3.1 per-step latency for the 4B/9B sizes on GB10 (only the 35B-A3B has vendor Spark numbers).

---

## 14. Sources
LLMs / benchmarks
- BFCL v4 leaderboard + data: https://gorilla.cs.berkeley.edu/leaderboard.html , https://gorilla.cs.berkeley.edu/data_overall.csv
- llm-stats BFCL-V4 (self-reported): https://llm-stats.com/benchmarks/bfcl-v4
- Qwen3.5-9B card: https://huggingface.co/Qwen/Qwen3.5-9B · Qwen3.6-35B-A3B card: https://huggingface.co/Qwen/Qwen3.6-35B-A3B · Qwen repo: https://github.com/QwenLM/Qwen3.8
- Nemotron 3.5 Lightning cards: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 , https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
- Nemotron 3 Nano tech report: https://arxiv.org/html/2512.20848v1 · gpt-oss model card: https://arxiv.org/pdf/2508.10925
- Gemma 4 releases: https://ai.google.dev/gemma/docs/releases · Granite 4.2: https://www.ibm.com/granite/docs/models/granite4-2 · LFM2.5: https://unsloth.ai/docs/models/lfm2.5
- FunctionGemma: https://blog.google/technology/developers/functiongemma/
- Spark speed: https://dev.classmethod.jp/articles/nvidia-nemotron-3-5-lightning-dspark-dflash-mtp-dgx-spark/ ,
  https://dev.classmethod.jp/en/articles/dgx-spark-qwen3-6-35b-a3b-nvfp4-new-champion/ ,
  https://forums.developer.nvidia.com/t/measured-inference-benchmarks-on-a-single-dgx-spark-same-harness-across-ollama-llama-cpp-and-vllm-notes-data-published/379766 ,
  https://ollama.com/blog/nvidia-spark-performance , https://github.com/ggml-org/llama.cpp/discussions/16578 ,
  https://forums.developer.nvidia.com/t/help-running-nemotron-3-nano-30b-a3b-fp8-on-dgx-spark-gb10/354771 ,
  https://huggingface.co/doth4580/Qwen3.5-9B-NVFP4-FULL , https://ai-muninn.com/en/blog/dgx-spark-gemma4-26b-nvfp4-52-toks
- vLLM on Spark: https://vllm.ai/blog/2026-06-01-vllm-dgx-spark · tool calling docs: https://github.com/vllm-project/vllm/blob/main/docs/features/tool_calling.md · sleep mode: https://github.com/vllm-project/vllm/blob/main/docs/features/sleep_mode.md
- Ollama: https://ollama.com/search?c=tools , https://ollama.com/library/qwen3.5 , https://ollama.com/library/gemma4 , https://ollama.com/library/gemma3 , https://ollama.com/library/gpt-oss , https://ollama.com/library/nemotron-3.5-lightning , https://ollama.com/library/qwen3 , https://ollama.com/library/phi4-mini , FAQ: https://raw.githubusercontent.com/ollama/ollama/main/docs/faq.mdx
- Qwen3.5 tool-calling issues: https://github.com/ollama/ollama/issues/14493 , https://github.com/vllm-project/vllm/pull/35347 , https://forums.developer.nvidia.com/t/qwen3-5-tool-calling-finally-fixed-possibly/366451

ASR / VAD / wake word / TTS
- Nemotron Speech Streaming EN 0.6B: https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b · NeMo-Speech.cpp: https://catalog.ngc.nvidia.com/orgs/nvidia/-/containers/nemo-speech.cpp
- Open ASR leaderboard paper: https://arxiv.org/abs/2510.06961 · aggregates: https://www.codesota.com/benchmark/open-asr-leaderboard , https://benchmarklist.com/benchmarks/open_asr_leaderboard/
- onnx-asr: https://pypi.org/project/onnx-asr/ · ORT on Spark: https://forums.developer.nvidia.com/t/onnx-runtime-gpu-inference-on-dgx-spark-gx10-build-guide-and-prebuilt-binaries/366157
- NeMo word boosting (GPU-PB): https://docs.nvidia.com/nemo/speech/latest/asr/asr_customization/word_boosting.html
- whisper.cpp README/VAD/server/command: https://github.com/ggml-org/whisper.cpp
- Silero VAD: https://github.com/snakers4/silero-vad
- Picovoice free-tier end: https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744
- openWakeWord: https://github.com/dscripka/openWakeWord · microWakeWord: https://github.com/OHF-Voice/micro-wake-word · pymicro-wakeword Debian: https://dfsg-new-queue.debian.org/reviews/python-pymicro-wakeword · Linux Voice Assistant: https://github.com/OHF-Voice/linux-voice-assistant
- PipeWire echo-cancel: https://docs.pipewire.org/page_module_echo_cancel.html
- Piper: https://github.com/OHF-Voice/piper1-gpl · Kokoro comparisons: https://www.promptquorum.com/power-local-llm/piper-vs-kokoro-tts , https://picovoice.ai/blog/on-device-tts/

Computer use
- Holo3.1: https://huggingface.co/blog/Hcompany/holo31 , https://hcompany.ai/newsroom/holo3.1 , https://huggingface.co/collections/Hcompany/holo31 , https://huggingface.co/Hcompany/Holo-3.1-4B
- Holo3: https://huggingface.co/Hcompany/Holo3-35B-A3B · Holo4: https://www.marktechpost.com/2026/09/29/h-company-releases-holo4-open-weight-computer-use-models-that-click-code-and-call-tools-across-desktop-web-android-and-apis/
- Fara1.5: https://huggingface.co/microsoft/Fara1.5-9B · Fara-7B: https://www.microsoft.com/en-us/research/blog/fara-7b-an-efficient-agentic-model-for-computer-use/
- OpenCUA: https://huggingface.co/xlangai/OpenCUA-7B · UI-TARS: https://github.com/bytedance/UI-TARS , https://huggingface.co/ByteDance-Seed/UI-TARS-1.5-7B
- TVWorld: https://arxiv.org/abs/2601.13142 · Qwen-UI-Agent: https://arxiv.org/abs/2607.28227 · OSWorld-Verified aggregates: https://benchlm.ai/benchmarks/osworld-verified

Harness / MCP / browser
- MCP 2026-07-28 spec: https://modelcontextprotocol.io/specification/2026-07-28/basic , /basic/transports/streamable-http , /basic/versioning , /server/discover , /server/tools
- MCP 2025-11-25 lifecycle: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
- Claude Code MCP docs: https://code.claude.com/docs/en/mcp
- SDKs: https://pypi.org/project/mcp/ , https://ts.sdk.modelcontextprotocol.io/v2/
- Chrome remote-debugging change: https://developer.chrome.com/blog/remote-debugging-port · Playwright MCP: https://github.com/microsoft/playwright-mcp
- Home Assistant AI/local-first: https://www.home-assistant.io/blog/2025/09/11/ai-in-home-assistant · hassil: https://github.com/home-assistant/hassil · Wyoming: https://github.com/OHF-Voice/wyoming · Conversation API: https://developers.home-assistant.io/docs/intent_conversation_api

Contention / platform
- Memory creep on Spark: https://forums.developer.nvidia.com/t/memory-creep-on-dgx-spark-where-your-128-gb-actually-goes-and-how-to-stop-it/364886
- NVIDIA vLLM Spark troubleshooting: https://build.nvidia.com/spark/vllm/troubleshooting
- GB10 CPU layout: https://storagereview.com/review/nvidia-dgx-spark-review-the-ai-appliance-bringing-datacenter-capabilities-to-desktops , https://old.chipsandcheese.com/?p=37766
- Siri Remote mic on Linux: https://github.com/azais-corentin/siri-remote , https://code.zanneth.com/zanneth/siriremote

## 15. Artifacts produced in this session (all in the scratchpad)
- `scratchpad/research/mcp_min.py` — dual-era MCP Streamable-HTTP server, stdlib, `--selftest` **[M]**.
- `scratchpad/research/tests/interop_sdk.py` — Python SDK 2.3.0 client interop (auto/modern/legacy) **[M]**.
- `scratchpad/research/tests/ts-client/interop.mjs` — TS SDK 2.3.1 client interop (auto/pin/legacy) **[M]**.
- `scratchpad/research/fastpath.py` — fast-path intent matcher + self-check + timing **[M]**.
