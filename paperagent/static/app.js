// Carousel for card-news previews
document.querySelectorAll("[data-carousel]").forEach((root) => {
  const track = root.querySelector(".slides");
  const imgs = track.querySelectorAll("img");
  const dots = root.querySelector(".dots");
  imgs.forEach(() => dots.appendChild(document.createElement("i")));
  const update = () => {
    const i = Math.round(track.scrollLeft / track.clientWidth);
    dots.querySelectorAll("i").forEach((d, j) => d.classList.toggle("on", i === j));
  };
  const go = (dir) => track.scrollBy({ left: dir * track.clientWidth, behavior: "smooth" });
  root.querySelector("[data-prev]").addEventListener("click", () => go(-1));
  root.querySelector("[data-next]").addEventListener("click", () => go(1));
  track.addEventListener("scroll", update, { passive: true });
  update();
});

// Copy caption text
document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const el = document.querySelector(btn.dataset.copy);
    try { await navigator.clipboard.writeText(el.value || el.textContent); }
    catch { el.select(); document.execCommand("copy"); }
    const old = btn.textContent; btn.textContent = "복사됨 ✓"; setTimeout(() => (btn.textContent = old), 1500);
  });
});

// Share: native share sheet on mobile, copy link elsewhere
document.querySelectorAll("[data-share]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const data = { title: btn.dataset.title, url: btn.dataset.share };
    if (navigator.share) { try { await navigator.share(data); return; } catch { /* cancelled */ } }
    try { await navigator.clipboard.writeText(data.url); btn.textContent = "링크 복사됨 ✓"; }
    catch { prompt("이 링크를 복사하세요", data.url); }
  });
});

// Confirm destructive actions
document.querySelectorAll("form[data-confirm]").forEach((f) => {
  f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
});

// While the agent runs, refresh the page when it finishes
if (document.querySelector(".live")) {
  const poll = setInterval(async () => {
    try {
      const r = await fetch("/api/status"); const s = await r.json();
      if (!s.running) { clearInterval(poll); location.reload(); }
    } catch { /* ignore */ }
  }, 15000);
}
