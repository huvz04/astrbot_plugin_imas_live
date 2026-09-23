const tasksNode = document.getElementById('tasks');
const budgetNode = document.getElementById('budget');
const activitiesNode = document.getElementById('activities');

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function show(data) {
  budgetNode.textContent = `SerpApi 本月请求 ${data.serpapi_requests}/${data.serpapi_budget} · 待发送 ${data.pending} · 密钥${data.provider_configured ? '已配置' : '未配置'}`;
  tasksNode.replaceChildren();
  if (!data.tasks.length) {
    tasksNode.append(element('section', '尚无机票计划。请先在目标会话使用 /imasflight 查询活动场次。', 'card'));
    return;
  }
  for (const task of data.tasks) {
    const card = element('article', undefined, 'card');
    card.append(element('h2', `#${task.event_number || '?'} ${task.event_title}`));
    card.append(element('span', task.enabled ? '监测中' : task.status, `pill ${task.enabled ? '' : 'warn'}`));
    card.append(element('p', `计划 ${task.id} · 目标 ${task.umo} · 心理价 ${task.target_price ? `CNY ${task.target_price}` : '未设置'} · ${task.baggage_requirement === 'checked' ? '必须含已核验托运' : '无托运限制'}`, 'muted'));
    card.append(element('p', `所选场次：${task.sessions.map(s => `${s.date} ${s.label || ''}`).join('；')}`));
    card.append(element('p', `抵达东京：${task.arrival_dates.join(' / ')}　返程：${task.return_dates.join(' / ')}`, 'muted'));
    if (!task.quotes.length) card.append(element('p', '尚无缓存的完整往返候选；空结果不代表无航班。', 'muted'));
    for (const quote of task.quotes) {
      const row = element('div', undefined, 'quote');
      row.append(element('div', `CNY ${Number(quote.price).toLocaleString()}`, 'price'));
      row.append(element('div', `${quote.origin} → ${quote.destination} ${quote.departure} / ${quote.arrival_date} 抵达；${quote.return_origin} → ${quote.return_destination} ${quote.return_departure_date} 返程`));
      row.append(element('div', `${quote.outbound_flights.join(' / ')} · ${quote.return_flights.join(' / ')} · 托运行李${quote.baggage === 'unknown' ? '待核实' : quote.baggage} · ${quote.stale ? '缓存陈旧或不符条件' : '缓存候选'}`, 'muted'));
      if (quote.link) {
        const link = element('a', '打开来源搜索结果');
        link.href = quote.link; link.target = '_blank'; link.rel = 'noopener noreferrer';
        row.append(link);
      }
      card.append(row);
    }
    for (const state of task.states.filter(s => s.error)) card.append(element('p', `查询 ${state.query.join(' / ')}：${state.error}`, 'muted'));
    tasksNode.append(card);
  }
}

function showActivities(data) {
  activitiesNode.replaceChildren();
  if (!data.activities.length) {
    activitiesNode.append(element('p', '尚无未来已收录场次。', 'muted'));
    return;
  }
  for (const activity of data.activities) {
    const item = element('details');
    item.append(element('summary', `#${activity.number || '?'} ${activity.title}`));
    for (const session of activity.sessions) {
      const line = element('p', `${session.date} · ${session.label || '场次待核验'} · ${session.venue || '场馆待核验'} · 场次ID ${session.id}${session.tokyo_route_supported ? '' : ' · 东京航线待核验'}`, 'muted');
      item.append(line);
    }
    activitiesNode.append(item);
  }
}

async function refresh() {
  try {
    const bridge = window.AstrBotPluginPage;
    if (!bridge) throw new Error('请从 AstrBot 插件详情页打开此页面。');
    const [status, activities] = await Promise.all([bridge.apiGet('flight/status'), bridge.apiGet('flight/activities')]);
    show(status);
    showActivities(activities);
  } catch (error) {
    budgetNode.textContent = `状态读取失败：${error.message}`;
  }
}

document.getElementById('refresh').addEventListener('click', refresh);
refresh();
