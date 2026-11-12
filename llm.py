"""LLM providers (Phase 12): send an assembled request (transcript with [n] markers + annotated images), get text back."""
import asyncio
import threading
import time
from pathlib import Path

from config import CONFIG
from log import log, log_exc


def build_prompt(request: dict, images: list[str], cfg=CONFIG) -> str:
    # Number images as actually sent (after any cap), so "image k" matches the attachment order.
    sent = {Path(p).name: k for k, p in enumerate(images, 1)}
    image_of = {c["id"]: sent[c["image"]] for c in request["contexts"] if c["image"] in sent}
    gestures = [f"[{g['index']}] {g['kind']}" + (f" in image {image_of[g['context']]}" if g.get("context") in image_of else "")
                for g in request["gestures"]]
    return cfg.llm_prompt_template.format(
        transcript=request["transcript_with_markers"],
        n_images=len(images),
        gestures="; ".join(gestures) if gestures else "none",
    )


def cap_images(images: list[str], limit: int | None, cfg=CONFIG) -> list[str]:
    if limit is None or len(images) <= limit:
        return images
    log("llm", f"model accepts {limit} image(s); dropping {len(images) - limit} (truncate_keep={cfg.truncate_keep!r})")
    return images[-limit:] if cfg.truncate_keep == "end" else images[:limit]


class LLMProvider:
    """Interface. start() returns immediately; respond() blocks (call it off the main thread)."""

    name = "base"
    model = ""

    def __init__(self, on_state=lambda state, detail: None, config=CONFIG):
        self.cfg = config
        self.on_state = on_state  # (state: "starting"|"ready"|"error", provider name, detail) from any thread
        self.ready = threading.Event()

    def start(self) -> None:
        self.ready.set()
        self.on_state("ready", self.name, self.model or "offline")

    def respond(self, request: dict) -> str:
        raise NotImplementedError

    def stop(self) -> None:
        pass


class NoneProvider(LLMProvider):
    """Nothing is sent; requests are only saved."""

    name = "none"

    def respond(self, request: dict) -> str:
        return ""


class EchoProvider(LLMProvider):
    """Offline stand-in: describes what would have been sent."""

    name = "echo"

    def respond(self, request: dict) -> str:
        images = cap_images(request["images"], None, self.cfg)
        prompt = build_prompt(request, images, self.cfg)
        return (f"(echo provider) {len(images)} image(s), {len(request['gestures'])} gesture(s).\n"
                f"Instruction: {request['transcript_with_markers']}\nPrompt length: {len(prompt)} chars.")


class CopilotProvider(LLMProvider):
    """GitHub Copilot SDK: one client on a dedicated asyncio thread, one tool-less session per request."""

    name = "copilot"

    def __init__(self, on_state=lambda state, detail: None, config=CONFIG):
        super().__init__(on_state, config)
        self.model = config.llm_model
        self.max_images = None
        self.error = None
        self._client = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="llm-loop", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.on_state("starting", self.name, self.model)
        asyncio.run_coroutine_threadsafe(self._start(), self._loop)

    async def _start(self) -> None:
        t0 = time.monotonic()
        try:
            from copilot import CopilotClient, StdioRuntimeConnection

            kwargs = {"log_level": "error"}
            if self.cfg.copilot_cli_path:
                kwargs["connection"] = StdioRuntimeConnection(path=self.cfg.copilot_cli_path)
            self._client = CopilotClient(**kwargs)
            await self._client.start()
            models = {m.id: m for m in await self._client.list_models()}
            if self.model not in models:
                raise RuntimeError(f"model {self.model!r} not available; choose one of {sorted(models)}")
            caps = getattr(models[self.model], "capabilities", None)
            if not getattr(getattr(caps, "supports", None), "vision", False):
                log("llm", f"WARNING model {self.model!r} does not support images; only text will be sent")
                self.max_images = 0
            else:
                vision = getattr(getattr(caps, "limits", None), "vision", None)
                self.max_images = getattr(vision, "max_prompt_images", None)
            self.ready.set()
            log("llm", f"copilot ready in {time.monotonic() - t0:.1f}s: model={self.model} max_images={self.max_images}")
            self.on_state("ready", self.name, self.model)
        except Exception as exc:
            self.error = str(exc)
            log_exc("llm", "copilot client failed to start")
            self.on_state("error", self.name, self.error)

    def respond(self, request: dict) -> str:
        if not self.ready.wait(self.cfg.llm_start_timeout_s):
            raise RuntimeError(self.error or f"Copilot not ready after {self.cfg.llm_start_timeout_s:.0f}s")
        future = asyncio.run_coroutine_threadsafe(self._respond(request), self._loop)
        return future.result(timeout=self.cfg.llm_timeout_s + 10)

    async def _respond(self, request: dict) -> str:
        images = cap_images(request["images"], self.max_images, self.cfg)
        prompt = build_prompt(request, images, self.cfg)
        session = await self._client.create_session(model=self.model, available_tools=[])  # text answer only
        try:
            event = await session.send_and_wait(
                prompt, attachments=[{"type": "file", "path": p} for p in images], timeout=self.cfg.llm_timeout_s
            )
            content = getattr(getattr(event, "data", None), "content", None)
            if not content:
                raise RuntimeError(f"no reply from {self.model} (event={getattr(event, 'type', None)})")
            return content.strip()
        finally:
            try:
                await session.disconnect()
            except Exception:
                pass

    def stop(self) -> None:
        if self._client is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._client.stop(), self._loop).result(timeout=5)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)


PROVIDERS = {"copilot": CopilotProvider, "echo": EchoProvider, "none": NoneProvider}


def make_provider(on_state, config=CONFIG) -> LLMProvider:
    cls = PROVIDERS.get(config.llm_provider)
    if cls is None:
        raise ValueError(f"unknown llm_provider {config.llm_provider!r}; choose one of {sorted(PROVIDERS)}")
    return cls(on_state, config)
