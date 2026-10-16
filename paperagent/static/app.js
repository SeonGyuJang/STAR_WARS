// Carousel for card-news previews
document.querySelectorAll("[data-carousel]").forEach((root) => {
  const track = root.querySelector(".slides");
  const items = [...track.children];
  const dots = root.querySelector(".dots");
  items.forEach((el) => {
    const d = document.createElement("i");
    if (el.tagName === "VIDEO") d.className = "vid";
    dots.appendChild(d);
  });
  const update = () => {
    const i = Math.round(track.scrollLeft / track.clientWidth);
    dots.querySelectorAll("i").forEach((d, j) => d.classList.toggle("on", i === j));
    // a video card plays while it is on screen
    items.forEach((el, j) => { if (el.tagName === "VIDEO") { if (i === j) el.play().catch(() => {}); else el.pause(); } });
  };
  const go = (dir) => track.scrollBy({ left: dir * track.clientWidth, behavior: "smooth" });
  root.querySelector("[data-prev]").addEventListener("click", () => go(-1));
  root.querySelector("[data-next]").addEventListener("click", () => go(1));
  track.addEventListener("scroll", update, { passive: true });
  update();
});

// Full-screen viewer for the card news: opens from "크게 보기" or a click on a card;
// arrows / swipe to move, Esc or the backdrop to close. The video cards play.
(() => {
  const carousels = document.querySelectorAll("[data-carousel]");
  if (!carousels.length) return;
  const box = document.createElement("div");
  box.className = "lightbox";
  box.hidden = true;
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-label", "카드뉴스 크게 보기");
  box.innerHTML = `
    <div class="lb-top"><span class="lb-count"></span><span class="lb-sp"></span>
      <a class="lb-dl" download>이 장 받기</a><button type="button" class="lb-x" aria-label="닫기">✕</button></div>
    <div class="lb-stage"></div>
    <button type="button" class="lb-btn lb-prev" aria-label="이전 장">‹</button>
    <button type="button" class="lb-btn lb-next" aria-label="다음 장">›</button>`;
  document.body.appendChild(box);
  const stage = box.querySelector(".lb-stage");
  let items = [], at = 0, track = null, lastFocus = null;

  const show = (i) => {
    at = (i + items.length) % items.length;
    const it = items[at];
    stage.replaceChildren();
    let el;
    if (it.tagName === "VIDEO") {
      el = document.createElement("video");
      Object.assign(el, { src: it.currentSrc || it.src, poster: it.poster, muted: true, loop: true, playsInline: true, autoplay: true });
      el.play().catch(() => {});
    } else {
      el = document.createElement("img");
      el.src = it.currentSrc || it.src;
      el.alt = it.alt || "";
    }
    stage.appendChild(el);
    box.querySelector(".lb-count").textContent = `${at + 1} / ${items.length}${it.tagName === "VIDEO" ? " · 영상" : ""}`;
    box.querySelector(".lb-dl").href = `${(it.currentSrc || it.src).split("?")[0]}?dl=1`;
    box.querySelector(".lb-prev").hidden = box.querySelector(".lb-next").hidden = items.length < 2;
  };
  const open = (root, i) => {
    track = root.querySelector(".slides");
    items = [...track.children];
    lastFocus = document.activeElement;
    box.hidden = false;
    document.body.classList.add("lb-open");
    show(i);
    box.querySelector(".lb-x").focus();
  };
  const close = () => {
    box.hidden = true;
    document.body.classList.remove("lb-open");
    stage.replaceChildren();
    if (track) track.scrollTo({ left: at * track.clientWidth }); // the small carousel follows
    lastFocus?.focus();
  };

  carousels.forEach((root) => {
    const tr = root.querySelector(".slides");
    const current = () => Math.round(tr.scrollLeft / tr.clientWidth);
    root.querySelector("[data-zoom]")?.addEventListener("click", () => open(root, current()));
    [...tr.children].forEach((el, i) => { el.style.cursor = "zoom-in"; el.addEventListener("click", () => open(root, i)); });
    root.addEventListener("keydown", (e) => { if (e.key === "f" || e.key === "F") open(root, current()); });
  });
  box.querySelector(".lb-x").addEventListener("click", close);
  box.querySelector(".lb-prev").addEventListener("click", () => show(at - 1));
  box.querySelector(".lb-next").addEventListener("click", () => show(at + 1));
  box.addEventListener("click", (e) => { if (e.target === box || e.target === stage) close(); });
  document.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(at - 1);
    else if (e.key === "ArrowRight") show(at + 1);
  });
  let x0 = null;
  stage.addEventListener("touchstart", (e) => { x0 = e.touches[0].clientX; }, { passive: true });
  stage.addEventListener("touchend", (e) => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 40) show(at + (dx < 0 ? 1 : -1));
    x0 = null;
  });
})();

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

// Run console: the header pill ("논문 읽는 중") opens a drawer with live progress,
// the papers of this run, the log (admin) and a force-stop button (admin).
(() => {
  const drawer = document.getElementById("console");
  if (!drawer) return;
  const $ = (sel) => drawer.querySelector(sel);
  const backdrop = document.querySelector(".console-backdrop");
  const pill = document.querySelector(".live");
  const admin = drawer.dataset.admin === "1";
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const PHASES = ["collect", "screen", "read", "trending", "tech", "done"];
  let timer = null, wasRunning = pill && !pill.hidden, followLog = true, selfScroll = false;

  const fmt = (s) => (s >= 3600 ? `${Math.floor(s / 3600)}시간 ` : "") + `${Math.floor((s % 3600) / 60)}분 ${s % 60}초`;
  const STATUS = { running: "실행 중", ok: "완료", partial: "AI 중단", error: "오류", aborted: "강제 종료됨" };

  function render(data) {
    const run = data.run;
    if (!run) { $("[data-c-meta]").textContent = "아직 실행 기록이 없습니다."; return; }
    const p = run.progress || {};
    const running = data.running;
    $("[data-c-meta]").textContent =
      `${STATUS[run.status] || run.status} · ${run.started_label} 시작 · ${running ? "경과" : "걸린 시간"} ${fmt(run.elapsed_s)}`;
    drawer.dataset.status = run.status;

    // stepper
    const at = running ? PHASES.indexOf(p.phase) : (run.status === "ok" || run.status === "partial" ? PHASES.length - 1 : PHASES.indexOf(p.phase));
    drawer.querySelectorAll("[data-step]").forEach((li) => {
      const i = PHASES.indexOf(li.dataset.step);
      li.className = i < at || (!running && i === at && run.status !== "aborted") ? "done" : i === at ? (running ? "now" : "stopped") : "";
    });

    // what is happening now
    const targets = p.targets || [];
    const done = p.done || [], failed = p.failed || [];
    const finished = done.length + failed.length;
    const now = $("[data-c-now]");
    now.hidden = !running;
    if (running) {
      const cur = targets.find((t) => t.id === p.current);
      $("[data-c-title]").textContent = cur ? cur.title : ({ collect: "새 논문 모으기", screen: "관련도 판단", trending: "트렌드 갱신", tech: "테크 소식 만들기" }[p.phase] || "준비 중");
      $("[data-c-stage]").textContent = p.stage ? `${p.stage}${p.model ? ` · ${p.model}` : ""}` : "";
      const pct = p.phase === "read" && targets.length ? Math.round(((finished + (cur ? 0.5 : 0)) / targets.length) * 100)
        : { collect: 5, screen: 12, trending: 90, tech: 95, done: 100 }[p.phase] || 3;
      $("[data-c-bar]").style.width = `${pct}%`;
      $("[data-c-count]").textContent = p.phase === "read" && targets.length ? `논문 ${finished}/${targets.length}편 완료` : "";
    }

    // the papers of this run
    $("[data-c-targets-wrap]").hidden = !targets.length;
    const list = $("[data-c-targets]");
    list.replaceChildren(...targets.map((t) => {
      const li = document.createElement("li");
      const state = done.includes(t.id) ? "done" : failed.includes(t.id) ? "failed" : t.id === p.current && running ? "now" : "wait";
      li.className = state;
      const a = document.createElement("a");
      a.href = `/paper/${t.id}`; a.textContent = t.title;
      const tag = document.createElement("span");
      tag.className = "st";
      tag.textContent = { done: "완료", failed: "실패", now: p.stage || "읽는 중", wait: running ? "대기" : "읽지 못함" }[state];
      li.append(a, tag);
      return li;
    }));

    // log
    const log = $("[data-c-log]");
    if (log && run.log) {
      if (log.childElementCount !== run.log.length || log.lastElementChild?.textContent !== run.log.at(-1)) {
        log.replaceChildren(...run.log.map((line) => { const d = document.createElement("div"); d.textContent = line; return d; }));
        if (followLog) { selfScroll = true; log.scrollTop = log.scrollHeight; }
      }
    }

    // controls and the header pill
    const stop = $("[data-c-stop]"), start = $("[data-c-start]");
    if (stop) stop.hidden = !running;
    if (start) start.hidden = running;
    if (pill) {
      pill.hidden = !running;
      pill.querySelector("[data-live-count]").textContent = running && p.phase === "read" && targets.length ? ` ${finished}/${targets.length}` : "";
    }
    if (wasRunning && !running && drawer.hidden) location.reload(); // new reviews to show
    wasRunning = running;
  }

  async function refresh() {
    try {
      const r = await fetch("/api/run/live", { headers: { Accept: "application/json" } });
      render(await r.json());
    } catch { /* keep the last view */ }
  }

  function schedule() {
    clearInterval(timer);
    const open = !drawer.hidden;
    if (open || wasRunning) timer = setInterval(refresh, open ? 2000 : 8000);
  }

  function open() {
    drawer.hidden = false; backdrop.hidden = false;
    document.body.classList.add("console-open");
    refresh(); schedule();
    drawer.querySelector("[data-console-close]").focus();
  }
  function close() {
    drawer.hidden = true; backdrop.hidden = true;
    document.body.classList.remove("console-open");
    schedule();
  }

  document.querySelectorAll("[data-console-open]").forEach((b) => b.addEventListener("click", open));
  document.querySelectorAll("[data-console-close]").forEach((b) => b.addEventListener("click", close));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) close(); });
  const log = $("[data-c-log]");
  if (log) log.addEventListener("scroll", () => {
    if (selfScroll) { selfScroll = false; return; }
    followLog = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    $("[data-c-follow]").textContent = followLog ? "실시간" : "일시정지 (맨 아래로 내리면 다시 따라갑니다)";
  });
  const stop = $("[data-c-stop]");
  if (stop && admin) stop.addEventListener("click", async () => {
    if (!confirm("실행을 강제 종료할까요? 지금 읽던 논문은 대기열로 돌아가고, 다음 실행 때 다시 읽습니다.")) return;
    stop.disabled = true;
    const body = new FormData(); body.append("_csrf", csrf);
    try { await fetch(drawer.dataset.stopUrl, { method: "POST", body, headers: { Accept: "application/json" } }); }
    finally { stop.disabled = false; refresh(); }
  });
  if (location.hash === "#console") open();
  else if (wasRunning) refresh(); // fill in "2/5" in the header right away
  schedule();
})();

// Messages: fade out after a few seconds (errors stay until closed)
document.querySelectorAll("[data-toast]").forEach((t) => {
  const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 300); };
  t.querySelector("[data-toast-close]").addEventListener("click", close);
  if (!t.classList.contains("error")) setTimeout(close, 6000);
});

// "/" jumps to the search box
document.addEventListener("keydown", (e) => {
  const search = document.querySelector("[data-search]");
  if (e.key === "/" && search && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
    e.preventDefault(); search.focus(); search.select();
  }
});

// Interest keywords: one line each with "..."; the toggle shows every keyword in full
document.querySelectorAll("[data-kw-list]").forEach((list) => {
  const btn = list.parentElement.querySelector("[data-kw-toggle]");
  if (!btn) return;
  const label = btn.textContent;
  const items = list.children;
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

// After an undo, the paper is shown (and briefly highlighted) on the main list
(() => {
  const hl = new URLSearchParams(location.search).get("hl");
  const el = hl && document.getElementById(`p-${hl}`);
  if (el) { el.classList.add("hl"); el.scrollIntoView({ block: "center" }); }
})();

// PDF upload: drag & drop, show what was picked, one title field only for a single file
document.querySelectorAll("[data-upload]").forEach((form) => {
  const drop = form.querySelector("[data-drop]");
  const input = drop.querySelector("input[type=file]");
  const label = form.querySelector("[data-drop-label]");
  const title = form.querySelector("[data-upload-title]");
  const btn = form.querySelector("[data-upload-btn]");
  const show = () => {
    const files = [...input.files];
    drop.classList.toggle("has-files", files.length > 0);
    label.textContent = files.length === 0 ? "PDF 파일을 끌어다 놓거나 눌러서 고르세요"
      : files.length === 1 ? files[0].name : `${files.length}개 파일: ${files.map((f) => f.name).join(", ")}`;
    title.disabled = files.length > 1;
    title.placeholder = files.length > 1 ? "여러 파일은 각 PDF에서 제목을 읽어 옵니다" : "논문 제목 (비워 두면 PDF에서 읽어 옵니다)";
  };
  input.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    const pdfs = [...e.dataTransfer.files].filter((f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name));
    const dt = new DataTransfer(); pdfs.forEach((f) => dt.items.add(f)); input.files = dt.files; show();
  });
  form.addEventListener("submit", () => { btn.disabled = true; btn.textContent = "올리는 중…"; });
});

