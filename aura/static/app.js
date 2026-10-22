const $ = (id) => document.getElementById(id);
const selectedEvents = new Set();
let catalog = { people: [], tags: [], events: [] },
  editing = null,
  baseUrl = "",
  visibleEvents = 20,
  previewVersion = 0,
  previewedQuery = null,
  eventPage = null;
async function api(path, options = {}) {
  const response = await fetch("/api/v1" + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.status === 204) return null;
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      typeof value.detail === "string"
        ? value.detail
        : JSON.stringify(value.detail),
    );
  return value;
}
function fail(error) {
  $("error").textContent = error.message;
  $("error").hidden = false;
}
function act(fn) {
  return async (...args) => {
    $("error").hidden = true;
    try {
      await fn(...args);
    } catch (error) {
      fail(error);
    }
  };
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text, fn) {
  const node = el("button", text);
  node.type = "button";
  node.onclick = act(fn);
  return node;
}
function link(text, path) {
  const node = el("a", text);
  node.href = baseUrl + path;
  node.target = "_blank";
  node.rel = "noopener";
  return node;
}
function photos(target, assets) {
  target.replaceChildren();
  assets.forEach((asset) => {
    const a = link("", "/photos/" + encodeURIComponent(asset.id));
    const img = el("img");
    img.src = "/api/v1/assets/" + encodeURIComponent(asset.id) + "/thumbnail";
    img.alt = "照片预览";
    img.loading = "lazy";
    img.onerror = () => {
      img.alt = "预览不可用，点击在 Immich 查看";
    };
    a.append(img);
    target.append(a);
  });
}
function selected(id) {
  return Array.from($(id).selectedOptions, (o) => o.value).filter(Boolean);
}
function formQuery() {
  const query = {};
  for (const [field, id] of [
    ["person_ids", "person-ids"],
    ["tag_ids", "tag-ids"],
  ]) {
    const values = selected(id);
    if (values.length) query[field] = values;
  }
  if ($("album-event").value) query.event = $("album-event").value;
  for (const key of ["start", "end"])
    if ($(key).value) query[key] = $(key).value + "T00:00:00Z";
  for (const [key, id] of [
    ["q", "semantic"],
    ["city", "city"],
    ["country", "country"],
    ["filename", "filename"],
  ])
    if ($(id).value.trim()) query[key] = $(id).value.trim();
  return query;
}
function invalidate() {
  previewVersion++;
  previewedQuery = null;
  $("save").disabled = true;
  $("preview-summary").textContent = "条件已更改，请重新预览。";
  $("preview").replaceChildren();
}
function setQuery(query) {
  query = { ...query, tag_ids: [...(query.tag_ids || [])] };
  const eventId =
    query.event ||
    query.tag_ids.find((id) => catalog.events.some((e) => e.id === id));
  if (eventId) {
    query.event = eventId;
    query.tag_ids = query.tag_ids.filter((id) => id !== eventId);
  }
  for (const [key, id] of [
    ["person_ids", "person-ids"],
    ["tag_ids", "tag-ids"],
  ]) {
    for (const value of query[key] || [])
      if (!Array.from($(id).options).some((o) => o.value === value)) {
        const option = el("option", "Immich 中不可见：" + value);
        option.value = value;
        $(id).append(option);
      }
    for (const option of $(id).options)
      option.selected = (query[key] || []).includes(option.value);
  }
  if (
    query.event &&
    !Array.from($("album-event").options).some(
      (o) => o.value === String(query.event),
    )
  ) {
    const option = el("option", "已删除或失效事件 #" + query.event);
    option.value = String(query.event);
    $("album-event").append(option);
  }
  $("album-event").value = query.event || "";
  for (const key of ["start", "end"])
    $(key).value = (query[key] || "").slice(0, 10);
  for (const [key, id] of [
    ["q", "semantic"],
    ["city", "city"],
    ["country", "country"],
    ["filename", "filename"],
  ])
    $(id).value = query[key] || "";
  invalidate();
}
function fillOptions(id, rows, text) {
  const prior = selected(id),
    target = $(id);
  target.replaceChildren();
  rows.forEach((row) => {
    const option = el("option", text(row));
    option.value = String(row.id);
    option.selected = prior.includes(String(row.id));
    target.append(option);
  });
}
async function loadCatalog() {
  const results = await Promise.allSettled([
    api("/people"),
    api("/tags"),
    api("/events"),
  ]);
  results.forEach((result, i) => {
    if (result.status === "fulfilled")
      catalog[["people", "tags", "events"][i]] = result.value;
    else fail(result.reason);
  });
  const query = formQuery();
  fillOptions(
    "person-ids",
    catalog.people,
    (p) => p.name || "未命名人物 " + p.id.slice(0, 8),
  );
  fillOptions(
    "tag-ids",
    catalog.tags.filter((t) => !catalog.events.some((e) => e.id === t.id)),
    (t) => t.value || t.name,
  );
  fillOptions(
    "album-event",
    [
      { id: "", name: "不限" },
      ...catalog.events.filter((e) => e.review_state === "confirmed"),
    ],
    (e) => e.name,
  );
  setQuery(query);
  renderEvents();
}
function updateEventSelection() {
  $("event-selection").textContent = `已选 ${selectedEvents.size} 个事件`;
  $("merge-events").disabled = selectedEvents.size < 2;
}
function renderEvents() {
  const liveIds = new Set(catalog.events.map((e) => e.id));
  for (const id of selectedEvents)
    if (!liveIds.has(id)) selectedEvents.delete(id);
  const term = $("event-filter").value.trim().toLowerCase();
  const events = catalog.events.filter((e) =>
    `${e.name} ${e.starts_at || ""} ${e.ends_at || ""}`
      .toLowerCase()
      .includes(term),
  );
  $("events").replaceChildren();
  events.slice(0, visibleEvents).forEach((event) => {
    const card = el("article", undefined, "card");
    const selection = el("label", undefined, "event-choice");
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedEvents.has(event.id);
    checkbox.onchange = () => {
      if (checkbox.checked) selectedEvents.add(event.id);
      else selectedEvents.delete(event.id);
      updateEventSelection();
    };
    selection.append(checkbox, el("strong", event.name));
    card.append(selection);
    const dates = [
      event.starts_at?.slice(0, 10),
      event.ends_at?.slice(0, 10),
    ].filter(Boolean);
    card.append(
      el(
        "p",
        `${event.asset_count} 张 · ${Array.from(new Set(dates)).join(" — ")}`,
        "muted",
      ),
    );
    if (event.issue) card.append(el("p", event.issue, "warning"));
    const name = el("input");
    name.value = event.name;
    name.maxLength = 200;
    name.setAttribute("aria-label", "事件名称");
    card.append(name);
    const actions = el("div", undefined, "actions");
    actions.append(
      button("预览", async () => {
        eventPage = { id: event.id, offset: 0 };
        $("event-title").textContent = event.name;
        $("event-dialog").showModal();
        await loadEventPage();
      }),
    );
    actions.append(
      button("保存名称", async () => {
        await api("/events/" + event.id, {
          method: "PATCH",
          body: JSON.stringify({ name: name.value }),
        });
        await loadCatalog();
        await loadAlbums();
      }),
    );
    actions.append(
      button("删除事件", async () => {
        if (
          !window.confirm(
            `删除事件“${event.name}”？对应的 Immich 事件标签将删除；照片和相册保留，相关规则暂停，原成员不会再次生成事件。`,
          )
        )
          return;
        await api("/events/" + event.id, { method: "DELETE" });
        selectedEvents.delete(event.id);
        $("events-status").textContent = "事件已删除，照片保留。";
        await loadCatalog();
        await loadAlbums();
      }),
    );
    card.append(actions);
    $("events").append(card);
  });
  if (!events.length) $("events").append(el("p", "没有匹配的事件。", "muted"));
  $("events-more").hidden = visibleEvents >= events.length;
  updateEventSelection();
}
async function loadEventPage() {
  $("event-preview-status").textContent = "正在从 Immich 读取…";
  $("event-preview").replaceChildren();
  const result = await api(
    `/events/${eventPage.id}/assets?offset=${eventPage.offset}&limit=30`,
  );
  photos($("event-preview"), result.assets);
  $("event-preview-status").textContent =
    `共 ${result.total} 张可用照片 · 第 ${Math.floor(eventPage.offset / 30) + 1} 页`;
  $("event-more").hidden = eventPage.offset + 30 >= result.total;
}
function ruleText(query) {
  const parts = [];
  for (const [key, rows, label] of [
    ["person_ids", catalog.people, "人物"],
    ["tag_ids", catalog.tags, "标签"],
  ])
    if (query[key]?.length)
      parts.push(
        label +
          "：" +
          query[key]
            .map((id) => {
              const row = rows.find((r) => r.id === id);
              return row?.value || row?.name || id.slice(0, 8);
            })
            .join("、"),
      );
  if (query.event)
    parts.push(
      "事件：" +
        (catalog.events.find((e) => e.id === query.event)?.name ||
          "#" + query.event),
    );
  for (const [key, label] of [
    ["start", "从"],
    ["end", "至"],
    ["city", "城市"],
    ["country", "国家"],
    ["q", "画面"],
    ["filename", "文件名"],
  ])
    if (query[key]) parts.push(label + "：" + query[key]);
  return parts.join(" · ") || "全部可用照片";
}
async function loadAlbums() {
  const albums = await api("/smart-albums");
  $("albums").replaceChildren();
  albums.forEach((album) => {
    const card = el("article", undefined, "card");
    card.append(el("h3", album.name), el("p", ruleText(album.query), "muted"));
    card.append(
      el(
        "p",
        `${album.matched_assets ?? "尚未计算"} 张（上次计算） · ${album.sync_mode === "mirror" ? "自动增减" : "只追加"} · ${album.materialized_at ? new Date(album.materialized_at).toLocaleString() : "等待更新"}`,
      ),
    );
    if (album.migration_issue || album.error)
      card.append(el("p", album.migration_issue || album.error, "warning"));
    const actions = el("div", undefined, "actions");
    actions.append(
      button("编辑条件", async () => {
        editing = album.id;
        $("form-title").textContent = "编辑相册规则";
        $("album-name").value = album.name;
        $("sync-mode").value = album.sync_mode;
        setQuery(album.query);
        $("cancel").hidden = false;
        $("album-form").scrollIntoView({ behavior: "smooth" });
      }),
    );
    if (!album.migration_issue)
      actions.append(
        button("立即更新", async () => {
          await api(`/smart-albums/${album.id}/materialize`, {
            method: "POST",
          });
          await loadAlbums();
        }),
      );
    actions.append(
      button("删除规则", async () => {
        if (
          !window.confirm(
            `删除规则“${album.name}”？将停止自动更新，照片和 Immich 相册保留。`,
          )
        )
          return;
        await api(`/smart-albums/${album.id}`, { method: "DELETE" });
        if (editing === album.id) resetForm();
        await loadAlbums();
      }),
    );
    if (album.remote_id)
      actions.append(
        link(
          "在 Immich 中打开 ↗",
          "/albums/" + encodeURIComponent(album.remote_id),
        ),
      );
    card.append(actions);
    $("albums").append(card);
  });
  if (!albums.length) $("albums").append(el("p", "还没有相册规则。", "muted"));
}
async function loadStatus() {
  const [connection, tasks] = await Promise.all([
    api("/integrations/immich"),
    api("/tasks"),
  ]);
  $("connection").textContent = !connection.configured
    ? "请配置 Immich API key。"
    : !connection.reachable
      ? connection.error
      : `Immich ${connection.version} · ${connection.compatible ? "版本兼容" : "尚未验证，自动任务和写入暂停"}`;
  $("tasks").replaceChildren();
  tasks.tasks.forEach((task) => {
    const row = el(
      "p",
      `${task.name === "events" ? "事件生成" : task.name === "event_tags" ? "事件标签任务" : task.name === "migration" ? "旧版命名迁移" : "相册更新"}：${{ complete: "完成", failed: "失败", running: "进行中", interrupted: "等待重试" }[task.state] || task.state}`,
    );
    if (task.error) row.append(el("span", " · " + task.error, "warning"));
    if (task.result?.paused)
      row.append(el("span", ` · ${task.result.paused} 个规则待处理`));
    $("tasks").append(row);
  });
  if (!tasks.event_tags_enabled)
    $("tasks").append(
      el(
        "p",
        "事件标签写入尚未启用，请先验证图库支持 XMP 持久化（见安装说明）。",
        "warning",
      ),
    );
  if (tasks.event_migration && !tasks.event_migration.complete) {
    $("tasks").append(
      el(
        "p",
        `事件标签迁移：${tasks.event_migration.migrated} / ${tasks.event_migration.total}`,
      ),
    );
  }
  for (const operation of tasks.event_operations || []) {
    if (!operation.error) continue;
    const row = el("div", undefined, "warning");
    row.append(el("p", operation.error));
    if (!operation.id.startsWith("legacy-"))
      row.append(
        button("取消任务，保留已完成修改", async () => {
          if (
            !window.confirm(
              "取消此任务？已经写入 Immich 的修改会保留，后续可重新整理。",
            )
          )
            return;
          await api("/event-operations/" + operation.id, { method: "DELETE" });
          await loadCatalog();
          await loadStatus();
        }),
      );
    $("tasks").append(row);
  }
}
async function preview() {
  const serial = ++previewVersion,
    query = formQuery();
  $("save").disabled = true;
  $("preview-summary").textContent = "正在查询 Immich…";
  const result = await api("/smart-albums/preview", {
    method: "POST",
    body: JSON.stringify({ query }),
  });
  if (serial !== previewVersion) return;
  previewedQuery = JSON.stringify(query);
  $("save").disabled = false;
  $("preview-summary").textContent =
    `匹配 ${result.matched_assets} 张。` +
    (!result.matched_assets
      ? " 当前组合没有交集；仍可保存，等待以后符合条件的照片。"
      : "") +
    (Object.keys(result.condition_counts).length
      ? " 单条件数量：" +
        Object.entries(result.condition_counts)
          .map(
            ([k, v]) =>
              `${{ person_ids: "人物", tag_ids: "标签", event: "事件", start: "开始日期", end: "结束日期", q: "画面", city: "城市", country: "国家", filename: "文件名" }[k]} ${v}`,
          )
          .join("，")
      : "");
  photos($("preview"), result.assets);
}
$("album-form").addEventListener("input", invalidate);
$("preview-button").onclick = act(preview);
$("album-form").onsubmit = act(async (event) => {
  event.preventDefault();
  const query = formQuery();
  if (previewedQuery !== JSON.stringify(query))
    throw new Error("请先预览当前条件。");
  $("save").disabled = true;
  try {
    await api(editing ? "/smart-albums/" + editing : "/smart-albums", {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify({
        name: $("album-name").value,
        query,
        sync_mode: $("sync-mode").value,
      }),
    });
    resetForm();
    await loadAlbums();
  } catch (error) {
    $("save").disabled = false;
    throw error;
  }
});
function resetForm() {
  editing = null;
  $("album-form").reset();
  $("form-title").textContent = "创建相册规则";
  $("cancel").hidden = true;
  invalidate();
}
$("cancel").onclick = resetForm;
$("parse-form").onsubmit = act(async (event) => {
  event.preventDefault();
  const result = await api(
    "/search/parse?text=" + encodeURIComponent($("natural-text").value),
  );
  setQuery(result.query);
  $("parse-note").textContent =
    result.note +
    (result.ambiguous_names.length
      ? " 重名需手选：" + result.ambiguous_names.join("、")
      : "");
});
$("event-filter").oninput = () => {
  visibleEvents = 20;
  renderEvents();
};
$("clear-events").onclick = () => {
  selectedEvents.clear();
  renderEvents();
};
$("merge-events").onclick = act(async () => {
  const ids = Array.from(selectedEvents);
  const name = $("merge-name").value.trim();
  $("merge-events").disabled = true;
  try {
    const result = await api("/events/merge", {
      method: "POST",
      body: JSON.stringify({ event_ids: ids, ...(name ? { name } : {}) }),
    });
    const query = formQuery();
    if (ids.includes(query.event)) {
      query.event = result.id;
      setQuery(query);
    }
    selectedEvents.clear();
    $("merge-name").value = "";
    $("events-status").textContent =
      `已合并 ${ids.length} 个事件，共 ${result.asset_count} 张照片；${result.updated_albums} 个相册规则已关联到合并后的事件。`;
    await loadCatalog();
    await loadAlbums();
  } finally {
    updateEventSelection();
  }
});
$("events-more").onclick = () => {
  visibleEvents += 20;
  renderEvents();
};
$("event-more").onclick = act(async () => {
  eventPage.offset += 30;
  await loadEventPage();
});
$("close-event").onclick = () => $("event-dialog").close();
$("refresh").onclick = act(async () => {
  await Promise.all([loadCatalog(), loadStatus(), loadAlbums()]);
});
act(async () => {
  const config = await api("/config");
  const defaultUrl = new URL(location.origin);
  defaultUrl.port = String(config.immich_port);
  const publicUrl = new URL(config.immich_public_url || defaultUrl.href);
  if (!["http:", "https:"].includes(publicUrl.protocol))
    throw new Error("IMMICH_PUBLIC_URL 必须是 HTTP(S) 地址");
  baseUrl = publicUrl.href.replace(/\/$/, "");
  $("immich-link").href = baseUrl;
  // Migration holds the event lock; keep progress polling independent of it.
  setInterval(() => {
    Promise.all([loadStatus(), loadAlbums()]).catch(fail);
  }, 15000);
  await Promise.all([loadCatalog(), loadStatus(), loadAlbums()]);
  const migration = await api("/migration");
  if (migration) {
    $("migration").hidden = false;
    $("migration-info").append(
      el("p", migration.note),
      el(
        "p",
        `已保留 ${migration.events} 个事件和 ${migration.albums} 个规则；历史命名 ${migration.local_names.length} 项。归档文件：${migration.archive}`,
      ),
    );
    migration.local_names.forEach((p) =>
      $("migration-info").append(
        el("p", `旧命名：${p.name} → ${p.immich_id || "无 Immich 对应人物"}`),
      ),
    );
  }
})();
