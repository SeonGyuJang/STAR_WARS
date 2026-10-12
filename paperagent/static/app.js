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

// Interest keywords: one line each with "..."; the toggle shows every keyword in full
document.querySelectorAll("[data-kw-list]").forEach((list) => {
  const btn = list.parentElement.querySelector("[data-kw-toggle]");
  if (!btn) return;
  const label = btn.textContent;
  const items = list.querySelectorAll("li");
  const truncated = [...list.querySelectorAll(".t")].some((t) => t.scrollWidth > t.clientWidth + 1);
  if (items.length > 6 || truncated) btn.hidden = false;
  btn.addEventListener("click", () => {
    const open = list.classList.toggle("expanded");
    list.classList.toggle("collapsed", !open);
    btn.textContent = open ? "접기" : label;
  });
});

// Long lists (trend panels, queue sections): show the first N rows, then "전체 보기"
document.querySelectorAll("[data-more]").forEach((panel) => {
  const n = parseInt(panel.dataset.more, 10);
  const rows = [...panel.querySelectorAll(".trow")];
  if (rows.length <= n) return;
  const extra = rows.slice(n);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "more-btn";
  const label = `전체 보기 (${rows.length}개)`;
  const set = (open) => {
    extra.forEach((r) => (r.hidden = !open));
    btn.textContent = open ? "접기" : label;
    btn.setAttribute("aria-expanded", open);
  };
  btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-expanded") !== "true";
    set(open);
    if (!open) panel.scrollIntoView({ block: "nearest" });
  });
  rows[rows.length - 1].after(btn);
  set(location.hash === `#${panel.id}`);
});

// Queue: select all in a section
document.querySelectorAll("[data-pick-all]").forEach((box) => {
  box.addEventListener("change", () => {
    // only the rows on screen, so folded rows are never picked by surprise
    box.closest("form").querySelectorAll("input[name=paper_id]").forEach((c) => {
      if (!c.closest(".trow").hidden) c.checked = box.checked;
    });
  });
});

// Instagram "posted" mark: toggle in place without reloading the page
document.querySelectorAll("form[data-ig]").forEach((form) => {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    form.classList.add("busy");
    try {
      const r = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(r.status);
      const s = await r.json();
      form.classList.toggle("on", s.posted);
      form.querySelector("input[name=posted]").value = s.posted ? "0" : "1";
      form.querySelector("button").setAttribute("aria-pressed", s.posted);
      form.querySelector(".lbl").textContent = s.posted ? `인스타 업로드 완료 · ${s.label}` : "인스타 업로드 완료";
      document.querySelectorAll("[data-ig-count]").forEach((el) => (el.textContent = s.counts[el.dataset.igCount]));
    } catch {
      form.submit(); // fall back to a normal post
    } finally {
      form.classList.remove("busy");
    }
  });
});
