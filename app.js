/* app.js — LOL 克制查询 + 英雄推荐 + 符文出装 + AI 教练
   依赖注入的全局: CHAMPIONS, COUNTER, BUILD, TIER_DATA, META, LANE_POOL, ICON_BASE */
(function () {
  "use strict";

  // ---- 基础映射 ----
  const byKey = {}, bySlug = {};
  CHAMPIONS.forEach(c => { byKey[c.key] = c; bySlug[c.slug] = c; });

  const ROLES = [
    { id: "top",     ic: "🛡️", nm: "上路" },
    { id: "jungle",  ic: "🌳", nm: "打野" },
    { id: "mid",     ic: "✨", nm: "中路" },
    { id: "adc",     ic: "🏹", nm: "下路" },
    { id: "support", ic: "💎", nm: "辅助" },
  ];
  const ROLE_NAMES = {};
  ROLES.forEach(r => { ROLE_NAMES[r.id] = r.nm; });

  const state = {
    role: null,
    mainTab: "counter",
    oppKey: null,
    region: "global",
    tier: "diamond",
    tierRegion: "global",
    tierTier: "diamond",
    buildHeroKey: null,
    buildRole: null,
    aiSide: "ally",
    ally: [],
    enemy: [],
  };

  function iconUrl(c) {
    if (!c) return "";
    return ICON_BASE.replace(/\{slug\}/g, c.slug).replace(/\{id\}/g, c.id).replace(/\{key\}/g, c.key);
  }

  function matchChamp(c, q) {
    if (!q) return true;
    q = q.trim().toLowerCase();
    return (c.name && c.name.toLowerCase().includes(q)) ||
           (c.title && c.title.toLowerCase().includes(q)) ||
           (c.slug && c.slug.includes(q)) ||
           (c.id && c.id.toLowerCase().includes(q));
  }

  const $ = sel => document.querySelector(sel);

  // ---- 英雄详情/出装 跳转 ----
  function showBuild(heroKey, role) {
    state.buildHeroKey = heroKey;
    state.buildRole = role || state.role || "support";
    renderBuild();
    const card = $("#buildCard");
    card.classList.remove("hidden");
    card.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ========== 步骤1: 分路选择 ==========
  function renderLanes() {
    const box = $("#lanes");
    box.innerHTML = "";
    ROLES.forEach(r => {
      const d = document.createElement("div");
      d.className = "lane" + (state.role === r.id ? " active" : "");
      d.innerHTML = `<div class="ic">${r.ic}</div><div class="nm">${r.nm}</div>`;
      d.onclick = () => {
        state.role = r.id;
        state.oppKey = null;
        state.buildHeroKey = null;
        state.buildRole = r.id;
        renderLanes();
        renderMainTab();
        $("#buildCard").classList.add("hidden");
      };
      box.appendChild(d);
    });
  }

  // ========== 主标签切换 ==========
  function renderMainTab() {
    if (state.mainTab === "counter") {
      renderOppGrid();
      renderCounter();
    } else {
      renderTier();
    }
  }

  function switchMainTab(tab) {
    state.mainTab = tab;
    document.querySelectorAll("#mainTabs .mtab").forEach(t => t.classList.remove("active"));
    document.querySelector(`#mainTabs .mtab[data-tab="${tab}"]`).classList.add("active");
    if (tab === "counter") {
      $("#tabCounter").classList.remove("hidden");
      $("#tabTier").classList.add("hidden");
      $("#tabBuild").classList.add("hidden");
      state.oppKey = null;
      renderOppGrid();
      renderCounter();
    } else if (tab === "tier") {
      $("#tabCounter").classList.add("hidden");
      $("#tabTier").classList.remove("hidden");
      $("#tabBuild").classList.add("hidden");
      renderTier();
    } else if (tab === "build") {
      $("#tabCounter").classList.add("hidden");
      $("#tabTier").classList.add("hidden");
      $("#tabBuild").classList.remove("hidden");
      renderBuildGrid();
    }
  }

  // ========== 步骤2a: 克制查询 ==========

  // ====== 符文出装查询 (新Tab) ======
  function renderBuildGrid(filter) {
    const grid = $("#buildGrid");
    grid.innerHTML = "";
    if (!state.role) { grid.innerHTML = '<div class="empty">请先选择分路</div>'; return; }
    // 符文出装 Tab: 显示全部英雄, 优先排列当前分路有数据的
    const pool = (LANE_POOL[state.role] || []);
    const poolSet = new Set(pool);
    // 收集所有有 build 数据的英雄
    const hasBuild = new Set();
    for (const r of ["top","jungle","mid","adc","support"]) {
      for (const slug of Object.keys(BUILD[r] || {})) {
        const c = CHAMPIONS.find(c => c.slug === slug);
        if (c) hasBuild.add(c.key);
      }
    }
    const extSet = new Set([...poolSet, ...hasBuild]);
    let list = CHAMPIONS.slice().sort((a, b) => {
      // 有数据的排前面, 当前分路的更优先
      const aHas = hasBuild.has(a.key) ? 0 : 1;
      const bHas = hasBuild.has(b.key) ? 0 : 1;
      const aPool = poolSet.has(a.key) ? 0 : 1;
      const bPool = poolSet.has(b.key) ? 0 : 1;
      return aHas - bHas || aPool - bPool || a.name.localeCompare(b.name, "zh");
    });
    if (filter) list = list.filter(c => matchChamp(c, filter));
    list.forEach(c => {
      const d = document.createElement("div");
      d.className = "champ";
      // 标记当前分路 vs 其他分路
      const inPool = poolSet.has(c.key);
      d.style.opacity = inPool ? "1" : "0.55";
      d.innerHTML = `<img loading="lazy" src="${iconUrl(c)}" onerror="this.style.opacity=.2"><span>${c.name}</span>${inPool ? '' : '<span style=\"font-size:9px;color:var(--warn)\">其他</span>'}`;
      d.onclick = () => {
        state.buildHeroKey = c.key;
        state.buildRole = state.role || "support";
        renderBuild();
        const card = $("#buildCard");
        card.classList.remove("hidden");
        card.scrollIntoView({ behavior: "smooth", block: "center" });
      };
      grid.appendChild(d);
    });
    if (state.buildHeroKey) {
      $("#buildCard").classList.remove("hidden");
    } else {
      $("#buildCard").classList.add("hidden");
    }
  }

  function renderOppGrid(filter) {
    const grid = $("#oppGrid");
    grid.innerHTML = "";
    if (!state.role) { grid.innerHTML = '<div class="empty">请先选择分路</div>'; return; }
    const pool = (LANE_POOL[state.role] || []);
    const poolSet = new Set(pool);
    // 扩展池: LANE_POOL + 有 counter/build 数据的英雄
    const hasData = new Set();
    for (const slug of Object.keys((COUNTER[state.role] || {}))) {
      const c = CHAMPIONS.find(c => c.slug === slug);
      if (c) hasData.add(c.key);
    }
    for (const slug of Object.keys((BUILD[state.role] || {}))) {
      const c = CHAMPIONS.find(c => c.slug === slug);
      if (c) hasData.add(c.key);
    }
    const extSet = new Set([...poolSet, ...hasData]);
    let list = CHAMPIONS.slice().sort((a, b) => {
      const pa = extSet.has(a.key) ? 0 : 1, pb = extSet.has(b.key) ? 0 : 1;
      return pa - pb || a.name.localeCompare(b.name, "zh");
    });
    list = list.filter(c => matchChamp(c, filter));
    list.forEach(c => {
      const d = document.createElement("div");
      d.className = "champ" + (state.oppKey === c.key ? " sel" : "");
      d.innerHTML = `<img loading="lazy" src="${iconUrl(c)}" onerror="this.style.opacity=.2"><span>${c.name}</span>`;
      d.onclick = () => { state.oppKey = c.key; renderOppGrid(filter); renderCounter(); };
      grid.appendChild(d);
    });
  }

  const TIER_FALLBACK = ["diamond", "master", "grandmaster", "challenger"];

  function getCounters(role, oppKey, region, tier) {
    const opp = byKey[oppKey];
    if (!opp) return null;
    // 先尝试选定段位, 若无数据则回退到其他段位
    const tiers = [tier, ...TIER_FALLBACK.filter(t => t !== tier)];
    let node = null;
    for (const t of tiers) {
      node = (((COUNTER[role] || {})[opp.slug] || {})[region] || {})[t];
      if (node && node.length) break;
    }
    if (!node || !node.length) return null;
    const rows = node.map(r => {
      const c = byKey[r.opp];
      if (!c) return null;
      return {
        key: r.opp, champ: c,
        counterWin: +(100 - r.win).toFixed(2),
        games: r.games,
      };
    }).filter(Boolean);
    rows.sort((a, b) => b.counterWin - a.counterWin || b.games - a.games);
    return rows;
  }

  function wrClass(w) { return w >= 52 ? "g" : w <= 48 ? "b" : "n"; }

  function renderCounter() {
    const box = $("#counterResult");
    box.innerHTML = "";
    if (!state.role) return;
    if (!state.oppKey) { box.innerHTML = '<div class="empty">选择对方对位英雄后给出克制建议</div>'; return; }

    const opp = byKey[state.oppKey];
    const pool = new Set(LANE_POOL[state.role] || []);
    const rows = getCounters(state.role, state.oppKey, state.region, state.tier);
    if (!rows) {
      box.innerHTML = `<div class="empty">暂无 ${opp.name} 在该地区/段位的数据(可能未抓取或 op.gg 无样本)。</div>`;
      return;
    }
    const normal = rows.filter(r => pool.has(r.key));
    const offRole = rows.filter(r => !pool.has(r.key));
    const top5 = normal.slice(0, 5);
    const altHigh = offRole.slice(0, 3);

    let html = `<h3 style="color:var(--gold);font-size:14px;margin:16px 0 8px">
      克制 <b>${opp.name}</b> 的推荐(${regionName(state.region)} · ${tierName(state.tier)})</h3>`;

    html += '<div class="summary-top">';
    top5.forEach((r, i) => {
      html += `<div class="pick" onclick="window._showBuild(${r.key},'${state.role}')" title="点击查看${r.champ.name}的符文出装">
        <div class="rank">${i + 1}</div>
        <img src="${iconUrl(r.champ)}" onerror="this.style.opacity=.2">
        <div class="meta"><span class="n">${r.champ.name}<span class="tag build">出装▶</span></span>
        <span class="wr ${wrClass(r.counterWin)}">胜率 ${r.counterWin}% · ${r.games}局</span></div>
      </div>`;
    });
    if (!top5.length) html += '<div class="empty">该分路正常英雄中暂无明显克制项</div>';
    html += '</div>';

    if (altHigh.length) {
      html += `<div class="alt-note">⚡ 以下英雄非本分路常规选择, 但对位胜率很高, 供你自行判断:</div>`;
      html += '<div class="summary-top">';
      altHigh.forEach(r => {
        html += `<div class="pick" onclick="window._showBuild(${r.key},'${state.role}')" title="点击查看${r.champ.name}的符文出装">
          <img src="${iconUrl(r.champ)}" onerror="this.style.opacity=.2">
          <div class="meta"><span class="n">${r.champ.name}<span class="tag off">非常规</span><span class="tag build">出装▶</span></span>
          <span class="wr ${wrClass(r.counterWin)}">胜率 ${r.counterWin}% · ${r.games}局</span></div>
        </div>`;
      });
      html += '</div>';
    }

    html += `<h3 style="color:var(--gold);font-size:14px;margin:20px 0 6px">不同地区 / 段位明细</h3>`;
    html += '<div class="matrix-tabs" id="mxTabs"></div><div id="mxBody"></div>';
    box.innerHTML = html;
    renderMatrix();
  }

  function renderMatrix() {
    const regions = [["global", "全球"], ["kr", "韩国"], ["cn", "中国"]];
    const tiers = [["diamond", "钻石+"], ["master", "大师"], ["grandmaster", "宗师"], ["challenger", "王者"]];
    const tabs = $("#mxTabs"); if (!tabs) return;
    let cur = { region: state.region, tier: state.tier };
    tabs.innerHTML = "";
    regions.forEach(([rk, rn]) => tiers.forEach(([tk, tn]) => {
      const t = document.createElement("div");
      t.className = "t" + (rk === cur.region && tk === cur.tier ? " active" : "");
      t.textContent = `${rn}·${tn}`;
      t.onclick = () => { cur = { region: rk, tier: tk }; drawBody(rk, tk); [...tabs.children].forEach(x => x.classList.remove("active")); t.classList.add("active"); };
      tabs.appendChild(t);
    }));
    const pool = new Set(LANE_POOL[state.role] || []);
    function drawBody(region, tier) {
      const body = $("#mxBody");
      const rows = getCounters(state.role, state.oppKey, region, tier);
      if (!rows || !rows.length) { body.innerHTML = '<div class="empty">该地区/段位无数据</div>'; return; }
      const show = rows.slice(0, 8);
      let h = '<table><thead><tr><th>#</th><th>英雄</th><th>克制胜率</th><th>对局数</th></tr></thead><tbody>';
      show.forEach((r, i) => {
        const off = pool.has(r.key) ? "" : '<span class="tag off">非常规</span>';
        h += `<tr><td>${i + 1}</td>
          <td><div class="cell" style="cursor:pointer" onclick="window._showBuild(${r.key},'${state.role}')">
            <img src="${iconUrl(r.champ)}" onerror="this.style.opacity=.2">${r.champ.name}${off}</div></td>
          <td class="wr ${wrClass(r.counterWin)}">${r.counterWin}%</td>
          <td style="color:var(--muted)">${r.games}</td></tr>`;
      });
      h += '</tbody></table>';
      body.innerHTML = h;
    }
    drawBody(cur.region, cur.tier);
  }

  function regionName(r){return {global:"全球",kr:"韩国",cn:"中国"}[r]||r;}
  function tierName(t){return {diamond:"钻石+",master:"大师",grandmaster:"宗师",challenger:"王者"}[t]||t;}

  // ========== 步骤2b: 英雄推荐 (Tier) ==========
  function getTierList(role, region, tier) {
    const node = (((TIER_DATA[role] || {})[region] || {})[tier]);
    if (!node || !node.length) return null;
    return node.map(r => {
      const c = byKey[r.key];
      if (!c) return null;
      return { key: r.key, champ: c, rank: r.rank, tier: r.tier, win: r.win, pick: r.pick, games: r.games };
    }).filter(Boolean);
  }

  function renderTier() {
    const box = $("#tierResult");
    box.innerHTML = "";
    if (!state.role) { box.innerHTML = '<div class="empty">请先选择分路</div>'; return; }

    const region = state.tierRegion || "global";
    const tier = state.tierTier || "diamond";
    const rows = getTierList(state.role, region, tier);
    if (!rows || !rows.length) {
      box.innerHTML = `<div class="empty">暂无 ${regionName(region)} · ${tierName(tier)} 的 ${ROLE_NAMES[state.role]} 排名数据(可能未抓取)。</div>`;
      return;
    }

    const top5 = rows.slice(0, 5);
    let html = `<h3 style="color:var(--gold);font-size:14px;margin:16px 0 8px">
      🏆 ${ROLE_NAMES[state.role]} Top${top5.length} 英雄 (${regionName(region)} · ${tierName(tier)})</h3>`;

    html += '<div class="summary-top">';
    top5.forEach((r, i) => {
      const rankCls = i === 0 ? "t1" : i === 1 ? "t2" : i === 2 ? "t3" : "";
      html += `<div class="tier-pick" onclick="window._showBuild(${r.key},'${state.role}')" title="点击查看${r.champ.name}的符文出装">
        <div class="trank ${rankCls}">#${i + 1}</div>
        <img src="${iconUrl(r.champ)}" onerror="this.style.opacity=.2">
        <div class="tmeta">
          <span class="tn">${r.champ.name}<span class="tag build">出装▶</span></span>
          <span class="twr ${wrClass(r.win)}">胜率 ${r.win != null ? r.win.toFixed(1) + '%' : 'N/A'}</span>
          <span class="tstats">选取 ${r.pick != null ? r.pick.toFixed(1) + '%' : 'N/A'} · ${r.games}局</span>
        </div>
      </div>`;
    });
    html += '</div>';
    box.innerHTML = html;
  }

  // ========== 步骤3: 符文出装加点连招推荐 ==========
  function getBuild(heroKey, role) {
    const hero = byKey[heroKey];
    if (!hero) return null;
    role = role || state.role || "support";
    // 先查指定分路
    let node = ((BUILD[role] || {})[hero.slug]);
    if (node && Object.keys(node).length > 0 && node.runes && (node.runes.primary || (node.runes.perks || []).length)) {
      return node;
    }
    // 回退: 查所有分路, 优先有符文数据的
    const ROLES = ["top", "jungle", "mid", "adc", "support"];
    let fallbackRole = null;
    let fallbackNode = null;
    for (const r of ROLES) {
      if (r === role) continue;
      const n = ((BUILD[r] || {})[hero.slug]);
      if (n && Object.keys(n).length > 0) {
        const hasRunes = n.runes && (n.runes.primary || (n.runes.perks || []).length);
        if (hasRunes && !fallbackNode) {
          fallbackNode = n;
          fallbackRole = r;
          break;  // 优先有符文的
        }
        if (!fallbackNode) {
          fallbackNode = n;
          fallbackRole = r;
        }
      }
    }
    if (fallbackNode) {
      // 更新 buildRole 以便 UI 显示正确来源
      state._buildFromRole = fallbackRole;
      return fallbackNode;
    }
    return null;
  }

  function metaItem(id) {
    const m = (META.items || {})[String(id)];
    if (m) return m;
    return { name: `物品${id}`, icon: "" };
  }

  function metaSpell(id) {
    const m = (META.spells || {})[String(id)];
    if (m) return m;
    return { name: `技能${id}`, icon: "" };
  }

  function metaRune(id) {
    const m = (META.runes || {})[String(id)];
    if (m) return m;
    return { name: `符文${id}`, icon: "" };
  }

  function metaRuneTree(id) {
    const m = (META.runeTrees || {})[String(id)];
    if (m) return m;
    return { name: `系${id}`, icon: "" };
  }

  function renderBuild() {
    const card = $("#buildCard");
    const title = $("#buildTitle");
    const content = $("#buildContent");
    const roleSel = $("#buildRoleSel");

    if (!state.buildHeroKey) {
      card.classList.add("hidden");
      return;
    }

    const hero = byKey[state.buildHeroKey];
    if (!hero) { card.classList.add("hidden"); return; }

    card.classList.remove("hidden");
    const fromRole = state._buildFromRole;
    const roleNote = fromRole ? ` <span style="font-size:11px;color:var(--warn);font-weight:400">(数据来自${ROLE_NAMES[fromRole] || fromRole})</span>` : "";
    title.innerHTML = `<img src="${iconUrl(hero)}" onerror="this.style.opacity=.2" style="width:36px;height:36px;border-radius:8px">
      ${hero.name} · 符文出装推荐${roleNote}`;

    // 同步下拉框
    if (roleSel) {
      roleSel.value = state.buildRole || state.role || "support";
    }

    const build = getBuild(state.buildHeroKey, state.buildRole || state.role);
    if (!build) {
      content.innerHTML = '<div class="empty">暂无该英雄在此分路的符文出装数据(请先运行 scrape.py 抓取 build 数据, 或切换到有数据的分路/英雄)</div>';
      return;
    }

    let html = '<div class="build-panel">';

    // ---- 符文 (OP.GG 风格布局) ----
    html += '<div class="build-row"><div class="build-block" style="flex:2;min-width:340px"><div class="lbl">🎯 符文</div>';
    if (build.runes && (build.runes.primary || (build.runes.perks || []).length)) {
      const rt = build.runes;
      // 去重 (OP.GG 页面有时同一符文出现在多个 DOM 位置)
      const seen = new Set();
      const allPerks = (rt.perks || []).filter(p => { if (seen.has(p)) return false; seen.add(p); return true; });
      const allShards = (rt.shards || []).filter(s => { if (seen.has(s)) return false; seen.add(s); return true; });
      html += '<div class="rune-page"><div class="rune-trees">';
      // 主系 (前4个perk)
      if (rt.primary) {
        const pt = metaRuneTree(rt.primary);
        const pPerks = allPerks.slice(0, 4);
        html += '<div class="rune-tree-col">';
        html += `<div class="tree-header"><img src="${pt.icon}" title="${pt.name}" onerror="this.style.opacity=.2"><span>${pt.name}</span></div>`;
        // 第一个是基石, 用大图标
        if (pPerks[0]) {
          const km = metaRune(pPerks[0]);
          html += `<div class="rune-keystone"><img src="${km.icon}" title="${km.name}" onerror="this.style.opacity=.2"><span>${km.name}</span></div>`;
        }
        // 剩余3行 (跳过基石)
        for (let i = 1; i < 4; i++) {
          if (pPerks[i]) {
            const pm = metaRune(pPerks[i]);
            html += `<div class="rune-perk-row"><img src="${pm.icon}" title="${pm.name}" onerror="this.style.opacity=.2"><span>${pm.name}</span></div>`;
          }
        }
        html += '</div>';
      }
      // 副系 (perk 4-6, 去重后)
      if (rt.sub) {
        const st = metaRuneTree(rt.sub);
        const sPerks = allPerks.slice(4, 6);
        html += '<div class="rune-tree-col">';
        html += `<div class="tree-header"><img src="${st.icon}" title="${st.name}" onerror="this.style.opacity=.2"><span>${st.name}</span></div>`;
        for (let i = 0; i < 2; i++) {
          if (sPerks[i]) {
            const sm = metaRune(sPerks[i]);
            html += `<div class="rune-perk-row"><img src="${sm.icon}" title="${sm.name}" onerror="this.style.opacity=.2"><span>${sm.name}</span></div>`;
          }
        }
        html += '</div>';
      }
      html += '</div>'; // rune-trees
      // 属性碎片
      if (allShards.length) {
        html += '<div class="rune-shards-row">';
        allShards.forEach(sid => {
          const sm = metaRune(sid);
          html += `<div class="rune-shard-item"><img src="${sm.icon}" title="${sm.name}" onerror="this.style.opacity=.2"><span>${sm.name}</span></div>`;
        });
        html += '</div>';
      }
      html += '</div>'; // rune-page
    } else {
      html += '<span style="color:var(--muted);font-size:12px">未抓取</span>';
    }
    html += '</div>';

    // ---- 召唤师技能 ----
    html += '<div class="build-block"><div class="lbl">⚡ 召唤师技能</div><div class="icons">';
    if ((build.spells || []).length) {
      build.spells.forEach(sid => {
        const sm = metaSpell(sid);
        html += `<img src="${sm.icon}" title="${sm.name}" onerror="this.style.opacity=.2"><span class="txt">${sm.name}</span>`;
      });
    } else {
      html += '<span style="color:var(--muted);font-size:12px">未抓取</span>';
    }
    html += '</div></div>';
    html += '</div>'; // build-row

    // ---- 技能加点 ----
    html += '<div class="build-row">';
    html += '<div class="build-block"><div class="lbl">📈 技能加点顺序</div>';
    if ((build.skill_order || []).length) {
      html += '<div class="skill-seq">';
      build.skill_order.forEach(s => {
        html += `<span>${s}</span>`;
      });
      html += '</div>';
    }
    if ((build.skill_master || []).length) {
      html += `<div style="font-size:11px;color:var(--muted);margin-top:4px">优先级: ${build.skill_master.join(" > ")}</div>`;
    }
    if (!(build.skill_order || []).length) {
      html += '<span style="color:var(--muted);font-size:12px">未抓取</span>';
    }
    html += '</div>';
    html += '</div>';

    // ---- 出装 ----
    html += '<div class="build-row">';
    html += '<div class="build-block"><div class="lbl">🛡️ 核心装备</div><div class="icons">';
    if ((build.core_items || []).length) {
      build.core_items.forEach(iid => {
        const im = metaItem(iid);
        html += `<img src="${im.icon}" title="${im.name}" onerror="this.style.opacity=.2"><span class="txt">${im.name}</span>`;
      });
    } else { html += '<span style="color:var(--muted);font-size:12px">未抓取</span>'; }
    html += '</div></div>';

    html += '<div class="build-block"><div class="lbl">第四件</div><div class="icons">';
    if ((build.item4 || []).length) {
      build.item4.forEach(iid => {
        const im = metaItem(iid);
        html += `<img src="${im.icon}" title="${im.name}" onerror="this.style.opacity=.2"><span class="txt">${im.name}</span>`;
      });
    } else { html += '<span style="color:var(--muted);font-size:12px">-</span>'; }
    html += '</div></div>';

    html += '<div class="build-block"><div class="lbl">第五件</div><div class="icons">';
    if ((build.item5 || []).length) {
      build.item5.forEach(iid => {
        const im = metaItem(iid);
        html += `<img src="${im.icon}" title="${im.name}" onerror="this.style.opacity=.2"><span class="txt">${im.name}</span>`;
      });
    } else { html += '<span style="color:var(--muted);font-size:12px">-</span>'; }
    html += '</div></div>';
    html += '</div>';

    // ---- 连招 (预生成数据, 来自 gen_combos.py) ----
    html += '<div class="build-row"><div class="build-block" style="flex:2;min-width:280px"><div class="lbl">⚔️ 连招</div>';
    const combos = build.combos || [];
    if (combos.length) {
      combos.forEach(co => {
        const tag = co.type === "basic" ? "🟢 基础连招" : co.type === "medium" ? "🟡 进阶连招" : "🔴 高阶连招";
        html += `<div class="combo-block">
          <div class="ctag">${tag}</div>
          <div class="cseq">${co.sequence}</div>
          ${co.note ? `<div style="font-size:11px;color:var(--muted);margin-top:2px">${co.note}</div>` : ""}
        </div>`;
      });
    } else {
      html += '<span style="color:var(--muted);font-size:12px">暂无连招数据(运行 gen_combos.py 生成)</span>';
    }
    html += '</div></div>';

    html += '</div>'; // build-panel
    content.innerHTML = html;
  }

  // ========== 步骤4: AI 教练 ==========
  // API 配置 (Anthropic 原生格式, 通过本地 proxy 转发)
  const AI_BASE = "";  // 同源 (server.py 一体式服务器), 也可改为 http://localhost:8899 用独立 proxy
  const AI_KEY = "sk-fc2ccfdf96c84519bac7ec5a56bd087b";
  const AI_MODEL = "deepseek-v4-pro";

  function renderAiGrid(filter) {
    const grid = $("#aiGrid"); grid.innerHTML = "";
    CHAMPIONS.filter(c => matchChamp(c, filter))
      .sort((a, b) => a.name.localeCompare(b.name, "zh"))
      .forEach(c => {
        const d = document.createElement("div");
        d.className = "champ";
        d.innerHTML = `<img loading="lazy" src="${iconUrl(c)}" onerror="this.style.opacity=.2"><span>${c.name}</span>`;
        d.onclick = () => addToSide(c.key);
        grid.appendChild(d);
      });
  }

  function addToSide(key) {
    const arr = state.aiSide === "ally" ? state.ally : state.enemy;
    if (arr.includes(key)) return;
    if (arr.length >= 5) return;
    arr.push(key); renderSlots();
  }

  function renderSlots() {
    [["ally", "#allyRow"], ["enemy", "#enemyRow"]].forEach(([side, sel]) => {
      const row = $(sel); row.innerHTML = "";
      state[side].forEach((key, idx) => {
        const c = byKey[key];
        const d = document.createElement("div");
        d.className = "slot";
        d.innerHTML = `<img src="${iconUrl(c)}" title="${c.name}" onerror="this.style.opacity=.2"><div class="x">×</div>`;
        d.querySelector(".x").onclick = () => { state[side].splice(idx, 1); renderSlots(); };
        row.appendChild(d);
      });
    });
  }

  function buildPrompt() {
    const nm = ks => ks.map(k => byKey[k] ? `${byKey[k].name}(${byKey[k].title})` : k).join("、") || "(未选)";
    const roleNm = ROLES.find(r => r.id === state.role);
    let ctx = "";
    if (state.role && state.oppKey) {
      const rows = getCounters(state.role, state.oppKey, state.region, state.tier);
      if (rows) {
        const top = rows.slice(0, 5).map(r => `${r.champ.name} ${r.counterWin}%`).join("、");
        ctx = `\n参考(${regionName(state.region)}${tierName(state.tier)} 对位 ${byKey[state.oppKey].name} 的高克制英雄): ${top}`;
      }
    }
    return `你是一名英雄联盟高分段教练。请基于以下信息分析对局。
我的分路: ${roleNm ? roleNm.nm : "(未指定)"}
己方阵容: ${nm(state.ally)}
对方阵容: ${nm(state.enemy)}${ctx}

请给出(允许阵容不完整, 就已知信息分析):
1. 阵容强弱与团队定位对比(强势期/分推/开团/后期等)
2. 我这一路的对线注意事项与压制/避战思路
3. 团战站位与目标选择关键点
4. 整局的取胜思路与节奏建议
用简体中文, 分点清晰, 务实可执行。`;
  }

  async function runAi() {
    const out = $("#aiOut");
    out.classList.remove("hidden");
    if (!state.ally.length && !state.enemy.length) { out.textContent = "请至少选择 1 个英雄(己方或对方)。"; return; }

    const btn = $("#aiRun"); btn.disabled = true;
    out.innerHTML = '<span class="spin"></span>分析中…';
    try {
      const resp = await fetch(AI_BASE + "/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": AI_KEY,
          "anthropic-version": "2023-06-01",
          "anthropic-dangerous-direct-browser-access": "true",
        },
        body: JSON.stringify({
          model: AI_MODEL,
          max_tokens: 4000,
          messages: [{ role: "user", content: buildPrompt() }],
        }),
      });
      const data = await resp.json();
      if (!resp.ok) { out.textContent = "调用失败: " + JSON.stringify(data, null, 2); btn.disabled = false; return; }
      const text = (data.content || []).map(b => b.text || "").join("") || JSON.stringify(data);
      out.textContent = text;
    } catch (e) {
      out.textContent = "请求出错: " + e.message +
        "\n\n提示: AI 教练需要 server.py 运行中。请执行 python server.py 然后在浏览器中访问 http://localhost:8899";
    }
    btn.disabled = false;
  }

  // ========== 初始化 ==========
  function init() {
    renderLanes();
    renderMainTab();
    renderAiGrid();
    renderSlots();

    // 主标签切换
    document.querySelectorAll("#mainTabs .mtab").forEach(t => {
      t.onclick = () => switchMainTab(t.dataset.tab);
    });

    // 克制查询事件
    $("#oppSearch").addEventListener("input", e => renderOppGrid(e.target.value));
    $("#regionSel").addEventListener("change", e => { state.region = e.target.value; renderCounter(); });
    $("#tierSel").addEventListener("change", e => { state.tier = e.target.value; renderCounter(); });

    // 英雄推荐事件
    $("#tierRegionSel").addEventListener("change", e => { state.tierRegion = e.target.value; renderTier(); });
    $("#tierTierSel").addEventListener("change", e => { state.tierTier = e.target.value; renderTier(); });

    // 符文出装查询事件
    $("#buildSearch").addEventListener("input", e => renderBuildGrid(e.target.value));

    // 出装分路切换
    $("#buildRoleSel").addEventListener("change", e => {
      state.buildRole = e.target.value;
      renderBuild();
    });

    // AI
    $("#aiSearch").addEventListener("input", e => renderAiGrid(e.target.value));
    $("#aiRun").addEventListener("click", runAi);
    document.querySelectorAll("#sideTabs .t").forEach(t => {
      t.onclick = () => {
        state.aiSide = t.dataset.side;
        document.querySelectorAll("#sideTabs .t").forEach(x => x.classList.remove("active"));
        t.classList.add("active");
      };
    });

    // 暴露全局方法(供内联 onclick 调用)
    window._showBuild = showBuild;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
