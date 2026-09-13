// flow-miniapp/src/App.jsx
import { useState, useCallback, useEffect, useRef, useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  Handle,
  Position,
  MarkerType,
} from "reactflow";

const API_BASE = "https://flow.dialogengine.ru/api";

// ─── SVG-иконки (без эмодзи) ─────────────────────────────────────────────────
const Icons = {
  trigger: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M8 1L10.2 6H15L11 9.4 12.5 15 8 11.8 3.5 15 5 9.4 1 6H5.8L8 1Z"
        fill="currentColor" />
    </svg>
  ),
  message: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M2 2h12a1 1 0 011 1v8a1 1 0 01-1 1H5l-3 2V3a1 1 0 011-1z"
        stroke="currentColor" strokeWidth="1.5" fill="none" />
    </svg>
  ),
  input: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <rect x="1" y="4" width="14" height="8" rx="1.5"
        stroke="currentColor" strokeWidth="1.5" fill="none" />
      <path d="M4 8h3M4 6.5v3" stroke="currentColor" strokeWidth="1.5"
        strokeLinecap="round" />
    </svg>
  ),
  condition: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M8 1L15 8L8 15L1 8L8 1Z"
        stroke="currentColor" strokeWidth="1.5" fill="none" />
    </svg>
  ),
  delay: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" fill="none" />
      <path d="M8 4.5V8l2.5 2" stroke="currentColor" strokeWidth="1.5"
        strokeLinecap="round" />
    </svg>
  ),
  http: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.5" fill="none" />
      <path d="M1.5 8h13M8 1.5c-2 2-3 4-3 6.5s1 4.5 3 6.5M8 1.5c2 2 3 4 3 6.5s-1 4.5-3 6.5"
        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  ),
  end: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <rect x="1.5" y="1.5" width="13" height="13" rx="2"
        stroke="currentColor" strokeWidth="1.5" fill="none" />
      <rect x="5" y="5" width="6" height="6" rx="1" fill="currentColor" />
    </svg>
  ),
  back: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M10 3L5 8l5 5" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  plus: () => (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M7 1v12M1 7h12" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" />
    </svg>
  ),
  close: () => (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" />
    </svg>
  ),
  trash: () => (
    <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
      <path d="M2 4h11M5 4V2.5a.5.5 0 01.5-.5h4a.5.5 0 01.5.5V4M6 7v4M9 7v4"
        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M3 4l.8 8.5a.5.5 0 00.5.5h6.4a.5.5 0 00.5-.5L12 4"
        stroke="currentColor" strokeWidth="1.5" />
    </svg>
  ),
  bolt: () => (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
      <path d="M11 2L4 11h6l-1 7 7-9h-6l1-7z" fill="currentColor" />
    </svg>
  ),
  save: () => (
    <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
      <path d="M2 2h9l2 2v9a1 1 0 01-1 1H2a1 1 0 01-1-1V3a1 1 0 011-1z"
        stroke="currentColor" strokeWidth="1.4" fill="none" />
      <path d="M5 2v3h5V2M4 8h7" stroke="currentColor" strokeWidth="1.4"
        strokeLinecap="round" />
    </svg>
  ),
  setvar: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M3 5h10M3 8h6M3 11h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
      <circle cx="12.5" cy="10.5" r="2.5" stroke="currentColor" strokeWidth="1.3" fill="none"/>
      <path d="M14.5 12.5l1.5 1.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
    </svg>
  ),
  random: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M1 4h3l8 8h3M14 4h-3L9.5 5.5M6.5 10.5L4 13H1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M12 2l2 2-2 2M12 10l2 2-2 2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  buttons: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <rect x="1" y="2" width="14" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.4" fill="none"/>
      <rect x="1" y="9" width="14" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.4" fill="none"/>
      <path d="M5 4.5h6M5 11.5h6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
    </svg>
  ),
  report: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <rect x="2" y="1" width="12" height="14" rx="1.5" stroke="currentColor" strokeWidth="1.4" fill="none"/>
      <path d="M5 5h6M5 8h6M5 11h3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
      <circle cx="12" cy="12" r="3" fill="currentColor" opacity=".25" stroke="currentColor" strokeWidth="1.2"/>
      <path d="M11 12h2M12 11v2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  ),
  plus: () => (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
    </svg>
  ),
  send_to_admin: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M2 2h12a1 1 0 011 1v8a1 1 0 01-1 1H5l-3 2V3a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.4" fill="none"/>
      <path d="M8 5v4M6 7l2-2 2 2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  notify_admin: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M8 1.5A5.5 5.5 0 0113.5 7v2l1 2H1.5l1-2V7A5.5 5.5 0 018 1.5z" stroke="currentColor" strokeWidth="1.4" fill="none"/>
      <path d="M6.5 13a1.5 1.5 0 003 0" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
    </svg>
  ),
  open_ticket: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <rect x="1.5" y="3" width="13" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.4" fill="none"/>
      <path d="M5 8h6M8 5v6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
    </svg>
  ),
  jump_scenario: () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M2 8h9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
      <path d="M8 5l4 3-4 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx="13" cy="8" r="2" stroke="currentColor" strokeWidth="1.3" fill="none"/>
    </svg>
  ),
};

// ─── Цветовая палитра ─────────────────────────────────────────────────────────
const C = {
  bg:       "#040d0a",   // почти чёрный с зелёным оттенком
  surface:  "#0b1a14",   // основные карточки
  elevated: "#122010",   // поверхность выше уровнем
  border:   "#1a3328",   // разделители
  borderHi: "#2a5040",   // бордер при hover
  green:    "#22c55e",   // акцент — ярко-зелёный
  greenDim: "#16a34a",   // акцент темнее
  greenBg:  "#052510",   // фон зелёного бейджа
  text:     "#cde8d8",   // основной текст
  textMid:  "#7aaa8e",   // вторичный текст
  textDim:  "#3d6650",   // приглушённый
  danger:   "#ef4444",
  dangerBg: "#2d0808",
  warn:     "#f59e0b",
};

const NODE_TYPES_META = {
  trigger:      { label: "Триггер",        color: "#6366f1", Icon: Icons.trigger,      hint: "Точка входа в сценарий. Каждый сценарий начинается здесь." },
  message:      { label: "Сообщение",      color: C.green,   Icon: Icons.message,      hint: "Отправляет сообщение пользователю. Можно вставить прямую ссылку на фото (jpg/png)." },
  input:        { label: "Ввод",           color: C.warn,    Icon: Icons.input,        hint: "Задаёт вопрос и сохраняет ответ в переменную." },
  condition:    { label: "Условие",        color: "#8b5cf6", Icon: Icons.condition,    hint: "Разветвляет сценарий по условию (да / нет)." },
  delay:        { label: "Задержка",       color: "#6b7280", Icon: Icons.delay,        hint: "Пауза перед следующим шагом." },
  set_variable: { label: "Задать перем.",  color: "#0ea5e9", Icon: Icons.setvar,       hint: "Устанавливает переменную вручную (статическое значение)." },
  random_branch:{ label: "Случайный выбор",color: "#f97316", Icon: Icons.random,       hint: "Случайно выбирает одну из веток (A или B)." },
  buttons:      { label: "Кнопки",         color: "#a855f7", Icon: Icons.buttons,      hint: "Отправляет сообщение с инлайн-кнопками. Пользователь выбирает ветку." },
  report:       { label: "Отчёт",          color: "#0891b2", Icon: Icons.report,      hint: "Отправляет отчёт с переменными в чат администраторов." },
  end:          { label: "Конец",          color: "#374151", Icon: Icons.end,          hint: "Завершает сценарий." },
  plus: () => (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
    </svg>
  ),
  send_to_admin:{ label: "Ввод → Админ",   color: "#16a34a", Icon: Icons.send_to_admin, hint: "Отправляет значение переменной (ответ пользователя) в чат администраторов." },
  notify_admin: { label: "Уведомить адм.", color: "#0d9488", Icon: Icons.notify_admin,  hint: "Отправляет произвольное сообщение в чат администраторов." },
  open_ticket:  { label: "Открыть тикет", color: "#ca8a04", Icon: Icons.open_ticket,   hint: "Открывает обращение пользователя в чате администраторов (в конце сценария)." },
  jump_scenario:{ label: "Перейти к сцен.",color: "#7c3aed", Icon: Icons.jump_scenario, hint: "Завершает текущий сценарий и немедленно запускает другой." },
};

const TRIGGER_OPTIONS = [
  { value: "command",  label: "/команда",         hint: "Срабатывает при отправке команды, например /start" },
  { value: "button",   label: "Кнопка",           hint: "Срабатывает при нажатии кнопки с нужным текстом" },
  { value: "keyword",  label: "Ключевое слово",   hint: "Срабатывает, если сообщение содержит слово" },
  { value: "start",    label: "При /start",       hint: "Срабатывает при первом запуске бота" },
];

const CONDITION_OPERATORS = [
  { value: "eq",       label: "равно" },
  { value: "contains", label: "содержит" },
  { value: "regex",    label: "по шаблону (regex)" },
  { value: "gt",       label: "больше (>)" },
  { value: "lt",       label: "меньше (<)" },
];


// ─── Узел на канвасе ──────────────────────────────────────────────────────────
function FlowNode({ id, data, selected }) {
  const meta = NODE_TYPES_META[data.nodeType] || NODE_TYPES_META.message;
  const hasInput = data.nodeType !== "trigger";
  const hasOutput = data.nodeType !== "end";
  const isBranch = data.nodeType === "condition";
  const isRandom = data.nodeType === "random_branch";
  const isButtons = data.nodeType === "buttons";
  return (
    <div style={{
      background: selected ? C.elevated : C.surface,
      border: `2px solid ${selected ? meta.color : C.border}`,
      borderRadius: 10,
      minWidth: 180,
      fontFamily: "'Inter', system-ui, sans-serif",
      boxShadow: selected
        ? `0 0 0 3px ${meta.color}33, 0 4px 24px #0008`
        : `0 4px 16px #0006`,
      transition: "border-color .15s, box-shadow .15s",
    }}>
      {hasInput && (
        <Handle type="target" position={Position.Top} style={{
          background: C.borderHi, border: `2px solid ${C.surface}`,
          width: 12, height: 12, top: -7,
        }} />
      )}

      {/* Шапка */}
      <div style={{
        background: meta.color + "22",
        borderBottom: `1px solid ${meta.color}33`,
        padding: "7px 12px",
        borderRadius: "8px 8px 0 0",
        display: "flex", alignItems: "center", gap: 8,
      }}>
        <span style={{ color: meta.color, display: "flex" }}>
          <meta.Icon />
        </span>
        <span style={{ color: meta.color, fontSize: 11, fontWeight: 700,
          letterSpacing: ".06em", textTransform: "uppercase" }}>
          {meta.label}
        </span>
      </div>

      {/* Тело */}
      <div style={{ padding: "9px 12px", color: C.text, fontSize: 13, lineHeight: 1.4 }}>
        <NodeSummary type={data.nodeType} config={data.config || {}} />
      </div>

      {data.label && (
        <div style={{ padding: "0 12px 8px", color: C.textDim, fontSize: 11, fontStyle: "italic" }}>
          {data.label}
        </div>
      )}

      {hasOutput && !isBranch && !isRandom && !isButtons && (
        <Handle type="source" position={Position.Bottom} style={{
          background: meta.color, border: `2px solid ${C.surface}`,
          width: 12, height: 12, bottom: -7,
        }} />
      )}
      {isBranch && (
        <>
          <Handle type="source" id="true" position={Position.Bottom} style={{
            background: C.green, border: `2px solid ${C.surface}`,
            width: 12, height: 12, bottom: -7, left: "30%",
          }} />
          <Handle type="source" id="false" position={Position.Bottom} style={{
            background: C.danger, border: `2px solid ${C.surface}`,
            width: 12, height: 12, bottom: -7, left: "70%",
          }} />
          <div style={{ display: "flex", justifyContent: "space-between",
            padding: "4px 10px 8px", fontSize: 10 }}>
            <span style={{ color: C.green }}>ДА</span>
            <span style={{ color: C.danger }}>НЕТ</span>
          </div>
        </>
      )}
      {isRandom && (() => {
        const brs = data.config?.branches || ["A","B"];
        const colors = ["#fb923c","#a78bfa","#38bdf8","#4ade80","#f472b6","#facc15"];
        const step = 100 / (brs.length + 1);
        return (<>
          {brs.map((br, i) => (
            <Handle key={br} type="source" id={br} position={Position.Bottom} style={{
              background: colors[i % colors.length], border: `2px solid ${C.surface}`,
              width: 12, height: 12, bottom: -7, left: `${step*(i+1)}%`,
            }} />
          ))}
          <div style={{ display:"flex", justifyContent:"space-around",
            padding:"4px 10px 8px", fontSize:10, flexWrap:"wrap", gap:4 }}>
            {brs.map((br, i) => (
              <span key={br} style={{ color: colors[i % colors.length] }}>{br}</span>
            ))}
          </div>
        </>);
      })()}
      {isButtons && (() => {
        const allBtns = data.config?.buttons || [];
        if (!allBtns.length) return (
          <Handle type="source" position={Position.Bottom} style={{
            background: "#a855f7", border: `2px solid ${C.surface}`,
            width: 12, height: 12, bottom: -7,
          }} />
        );
        return (
          <div style={{ padding:"0 10px 10px", display:"flex", flexDirection:"column", gap:4 }}>
            {allBtns.map((btn, i) => {
              const txt = typeof btn==="string" ? btn : btn.text;
              const isUrl = typeof btn!=="string" && btn.type==="url";
              return (
                <div key={i} style={{
                  background: isUrl ? "#0e3a2a" : "#a855f722",
                  border: `1px solid ${isUrl ? "#1a6b48" : "#a855f744"}`,
                  borderRadius:4, padding:"3px 8px 3px 6px", fontSize:10,
                  color: isUrl ? "#4ade80" : "#d8b4fe",
                  position:"relative", display:"flex", alignItems:"center", justifyContent:"space-between",
                }}>
                  <span>{txt||"…"}{isUrl?" ↗":""}</span>
                  {!isUrl && (
                    <Handle type="source" id={`btn_${i}`} position={Position.Right} style={{
                      background:"#a855f7", border:`2px solid ${C.surface}`,
                      width:10, height:10, right:-6, top:"50%", transform:"translateY(-50%)",
                    }} />
                  )}
                </div>
              );
            })}
          </div>
        );
      })()}
    </div>
  );
}

function NodeSummary({ type, config }) {
  if (type === "trigger") return <span style={{ color: C.textMid }}>Точка входа</span>;
  if (type === "end") return <span style={{ color: C.textMid }}>Сценарий завершён</span>;
  if (type === "message") return (
    <span style={{ color: C.text }}>
      {config.text ? truncate(config.text, 60) : <em style={{ color: C.textDim }}>Текст не задан</em>}
    </span>
  );
  if (type === "input") return (
    <div style={{ fontSize: 12 }}>
      {(config.question || config.prompt)
        ? <div style={{ color: C.textMid, marginBottom: 2 }}>
            {truncate((config.question || config.prompt).replace(/<[^>]+>/g,""), 38)}
          </div>
        : null}
      <span style={{ color: C.warn }}>→ </span>
      <span style={{ color: C.text }}>
        {config.variable_name || <em style={{ color: C.textDim }}>задайте переменную</em>}
      </span>
    </div>
  );
  if (type === "condition") return (
    <span style={{ color: C.text }}>
      <span style={{ color: "#a78bfa" }}>{config.variable || "?"}</span>
      {" "}{CONDITION_OPERATORS.find(o => o.value === config.operator)?.label || "="}
      {" "}<span style={{ color: C.warn }}>"{config.value || "?"}"</span>
    </span>
  );
  if (type === "delay") return (
    <span style={{ color: C.textMid }}>{config.seconds || 0} сек</span>
  );
  if (type === "set_variable") return (
    <span style={{ color: "#7dd3fc" }}>
      <span style={{ color: "#38bdf8" }}>{config.variable_name || "перем."}</span>
      <span style={{ color: C.textMid }}> = </span>
      <span style={{ color: C.warn }}>"{truncate(config.value || "?", 24)}"</span>
    </span>
  );
  if (type === "random_branch") return (
    <span style={{ color: "#fdba74", fontSize: 12 }}>50% A · 50% B</span>
  );
  if (type === "buttons") return (
    <span style={{ color: "#d8b4fe", fontSize: 12 }}>
      {config.buttons?.length ? `${config.buttons.length} кнопк${config.buttons.length === 1 ? "а" : "и"}` : "Кнопки не заданы"}
    </span>
  );
  if (type === "report") return (
    <span style={{ color: "#67e8f9", fontSize: 12 }}>
      {config.variables?.length
        ? `${config.variables.length} перем. → чат администраторов`
        : <em style={{ color: C.textDim }}>Настройте переменные</em>}
    </span>
  );
  if (type === "send_to_admin") return (
    <span style={{ color: "#4ade80", fontSize: 12 }}>
      {config.variable_name
        ? <>{config.variable_name} <span style={{color:C.textDim}}>→ чат адм.</span></>
        : <em style={{ color: C.textDim }}>Выберите переменную</em>}
    </span>
  );
  if (type === "notify_admin") return (
    <span style={{ color: "#2dd4bf", fontSize: 12 }}>
      {config.text ? truncate(config.text, 28) : <em style={{ color: C.textDim }}>Введите текст</em>}
    </span>
  );
  if (type === "open_ticket") return (
    <span style={{ color: "#facc15", fontSize: 12 }}>
      {config.subject ? truncate(config.subject,28) : "Откроет обращение"}
    </span>
  );
  if (type === "jump_scenario") return (
    <span style={{ color: "#a78bfa", fontSize: 12 }}>
      {config.scenario_name
        ? <>{config.scenario_name}</>
        : <em style={{ color: C.textDim }}>Выберите сценарий</em>}
    </span>
  );
  return null;
}



// ─── RichTextArea: textarea с контекстным меню форматирования ────────────────
function RichTextArea({ value, onChange, placeholder, style }) {
  const ref = useRef(null);
  const [menu, setMenu] = useState(null); // { x, y }

  function wrapSelection(before, after = before) {
    const el = ref.current;
    if (!el) return;
    const { selectionStart: s, selectionEnd: e } = el;
    if (s === e) return; // нет выделения
    const text = el.value;
    const newVal = text.slice(0, s) + before + text.slice(s, e) + after + text.slice(e);
    onChange(newVal);
    // restore cursor after React re-render
    requestAnimationFrame(() => {
      el.selectionStart = s + before.length;
      el.selectionEnd = e + before.length;
      el.focus();
    });
  }

  function insertLink() {
    const el = ref.current;
    if (!el) return;
    const { selectionStart: s, selectionEnd: e } = el;
    const selected = el.value.slice(s, e) || "текст ссылки";
    const url = window.prompt("URL:", "https://");
    if (!url) return;
    const ins = `<a href="${url}">${selected}</a>`;
    const text = el.value;
    onChange(text.slice(0, s) + ins + text.slice(e));
    setMenu(null);
  }

  function handleContext(ev) {
    const sel = window.getSelection()?.toString() || ref.current?.value?.slice(
      ref.current.selectionStart, ref.current.selectionEnd) || "";
    if (!sel) return; // no selection — use default menu
    ev.preventDefault();
    setMenu({ x: ev.clientX, y: ev.clientY, has_sel: true });
  }

  // long-press for mobile
  const longPressTimer = useRef(null);
  function handleTouchStart() {
    longPressTimer.current = setTimeout(() => {
      const el = ref.current;
      const sel = el?.value?.slice(el.selectionStart, el.selectionEnd) || "";
      if (sel) setMenu({ x: window.innerWidth / 2 - 80, y: 200, has_sel: true });
    }, 600);
  }
  function handleTouchEnd() { clearTimeout(longPressTimer.current); }

  const menuActions = [
    { label: "Жирный", fn: () => { wrapSelection("<b>", "</b>"); setMenu(null); } },
    { label: "Курсив",  fn: () => { wrapSelection("<i>", "</i>"); setMenu(null); } },
    { label: "Моно",   fn: () => { wrapSelection("<code>", "</code>"); setMenu(null); } },
    { label: "Зачёрк.", fn: () => { wrapSelection("<s>", "</s>"); setMenu(null); } },
    { label: "Ссылка",  fn: insertLink },
  ];

  return (
    <>
      <textarea
        ref={ref}
        style={style}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        onContextMenu={handleContext}
        onTouchStart={handleTouchStart}
        onTouchEnd={handleTouchEnd}
        onKeyDown={e => { if(e.key==="Enter") e.stopPropagation(); }}
      />
      {menu && (
        <>
          <div onClick={() => setMenu(null)}
            style={{ position:"fixed",inset:0,zIndex:499 }} />
          <div style={{
            position: "fixed", left: menu.x, top: menu.y,
            background: C.surface, border: `1px solid ${C.borderHi}`,
            borderRadius: 8, zIndex: 500, overflow: "hidden",
            boxShadow: "0 4px 20px #0008",
            fontFamily: "inherit",
          }}>
            {menuActions.map(a => (
              <button key={a.label} onClick={a.fn} style={{
                display: "block", width: "100%", textAlign: "left",
                background: "none", border: "none", padding: "9px 16px",
                color: C.text, fontSize: 13, cursor: "pointer",
                fontFamily: "inherit",
              }}
              onMouseEnter={e => e.currentTarget.style.background = C.elevated}
              onMouseLeave={e => e.currentTarget.style.background = "none"}
              >{a.label}</button>
            ))}
          </div>
        </>
      )}
    </>
  );
}

// ─── TagListEditor: кнопки «Добавить» вместо textarea ─────────────────────────
function TagListEditor({ items, onChange, placeholder = "Новый элемент", addLabel = "+ Добавить" }) {
  const [draft, setDraft] = useState("");
  function add() {
    const v = draft.trim();
    if (!v) return;
    onChange([...items, v]);
    setDraft("");
  }
  return (
    <div>
      {items.map((item, i) => (
        <div key={i} style={{ display:"flex", alignItems:"center", gap:6, marginBottom:5 }}>
          <input
            style={{ ...inputStyle, flex:1, margin:0 }}
            value={item}
            onChange={e => { const a=[...items]; a[i]=e.target.value; onChange(a); }}
          />
          <button onClick={() => onChange(items.filter((_,j)=>j!==i))}
            style={{ ...iconBtnStyle, color: C.danger, flexShrink:0 }}>
            <Icons.close />
          </button>
        </div>
      ))}
      <div style={{ display:"flex", gap:6, marginTop:4 }}>
        <input style={{ ...inputStyle, flex:1, margin:0 }}
          value={draft} placeholder={placeholder}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => e.key==="Enter" && (e.preventDefault(), add())} />
        <button onClick={add}
          style={{ ...btnStyle(C.elevated, C.green), flexShrink:0, display:"flex", alignItems:"center", gap:5 }}>
          <Icons.plus /> Добавить
        </button>
      </div>
    </div>
  );
}

// ─── ButtonsEditor: список кнопок с типом (ветка / ссылка) ───────────────────
function ButtonsEditor({ buttons, onChange }) {
  // button: { text, type: "branch"|"url", url? }
  const normalized = (buttons || []).map(b =>
    typeof b === "string" ? { text: b, type: "branch" } : b
  );

  function update(i, patch) {
    const a = normalized.map((x,j)=>j===i?{...x,...patch}:x);
    onChange(a);
  }
  function remove(i) { onChange(normalized.filter((_,j)=>j!==i)); }
  function add() { onChange([...normalized, { text: "", type: "branch" }]); }

  return (
    <div>
      {normalized.map((btn, i) => (
        <div key={i} style={{ background:C.elevated, borderRadius:8, padding:"8px 10px",
          marginBottom:8, border:`1px solid ${C.border}` }}>
          <div style={{ display:"flex", alignItems:"center", gap:6, marginBottom:btn.type==="url"?6:0 }}>
            <input style={{ ...inputStyle, flex:1, margin:0 }}
              value={btn.text} placeholder="Текст кнопки"
              onChange={e => update(i, { text: e.target.value })} />
            <select style={{ ...inputStyle, width:90, margin:0, flexShrink:0 }}
              value={btn.type}
              onChange={e => update(i, { type: e.target.value, url: undefined })}>
              <option value="branch">Ветка</option>
              <option value="url">Ссылка</option>
            </select>
            <button onClick={() => remove(i)} style={{ ...iconBtnStyle, color:C.danger, flexShrink:0 }}>
              <Icons.close />
            </button>
          </div>
          {btn.type === "url" && (
            <input style={{ ...inputStyle, margin:0 }}
              value={btn.url||""} placeholder="https://..."
              onChange={e => update(i, { url: e.target.value })} />
          )}
          {btn.type === "branch" && (
            <div style={{ fontSize:10, color:C.textDim, marginTop:4 }}>
              Создаст выход из узла — подключите его к следующему шагу
            </div>
          )}
        </div>
      ))}
      <button onClick={add} style={{ ...btnStyle(C.elevated, C.green),
        display:"flex", alignItems:"center", gap:6, width:"100%" }}>
        <Icons.plus /> Добавить кнопку
      </button>
    </div>
  );
}

// ─── VariablePicker: выбор переменных из узлов Ввод ───────────────────────────
function VariablePicker({ selectedVars, onChange, allVars }) {
  return (
    <div>
      {allVars.length === 0 && (
        <div style={{ color:C.textDim, fontSize:12 }}>
          Нет узлов «Ввод» в сценарии. Добавьте их чтобы выбирать переменные.
        </div>
      )}
      {allVars.map(v => {
        const checked = selectedVars.includes(v);
        return (
          <label key={v} style={{ display:"flex", alignItems:"center", gap:8,
            color:C.text, fontSize:13, marginBottom:8, cursor:"pointer" }}>
            <input type="checkbox" checked={checked}
              style={{ accentColor:C.green, width:14, height:14 }}
              onChange={() => {
                if (checked) onChange(selectedVars.filter(x=>x!==v));
                else onChange([...selectedVars, v]);
              }} />
            <code style={{ background:C.elevated, padding:"2px 6px", borderRadius:4, color:C.green }}>
              {"{{"}{v}{"}}"}
            </code>
          </label>
        );
      })}
    </div>
  );
}

// ─── Панель настроек узла (боковая) ──────────────────────────────────────────
function ConfigPanel({ node, onChange, onClose, onDelete, isMobile, allNodes }) {
  const { nodeType: type, config = {}, label } = node.data;
  const meta = NODE_TYPES_META[type] || {};

  // Собираем переменные из всех узлов типа "input" в сценарии
  const allVars = useMemo(() =>
    (allNodes || [])
      .filter(n => n.data?.nodeType === "input" && n.data?.config?.variable_name)
      .map(n => n.data.config.variable_name),
    [allNodes]
  );

  // Список сценариев для jump_scenario
  const [scenarios, setScenarios] = useState([]);
  useEffect(() => {
    if (type !== "jump_scenario") return;
    fetch(`${API_BASE}/scenarios_list`, { headers: { "X-Bot-Id": node.botId || "" } })
      .then(r => r.ok ? r.json() : [])
      .then(list => setScenarios(list || []))
      .catch(() => {});
  }, [type]);

  function set(key, val) {
    onChange({ ...config, [key]: val }, label);
  }

  const panelStyle = isMobile ? {
    position: "fixed", bottom: 0, left: 0, right: 0,
    background: C.surface, borderTop: `1px solid ${C.border}`,
    zIndex: 300, padding: "14px 14px 20px", maxHeight: "65vh",
    overflowY: "auto", fontFamily: "'Inter', system-ui, sans-serif",
  } : {
    position: "fixed", top: 60, right: 0, width: 310, bottom: 0,
    background: C.surface, borderLeft: `1px solid ${C.border}`,
    zIndex: 300, padding: "16px 16px 24px", overflowY: "auto",
    fontFamily: "'Inter', system-ui, sans-serif",
  };

  const taStyle = {
    ...inputStyle, width: "100%", minHeight: 90, resize: "vertical",
    lineHeight: 1.5, fontFamily: "monospace", boxSizing: "border-box",
    display: "block",
  };

  return (
    <div style={panelStyle}>
      {/* Заголовок */}
      <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:14 }}>
        <span style={{ color: meta.color || C.green, display:"flex", alignItems:"center" }}>
          {meta.Icon && <meta.Icon />}
        </span>
        <span style={{ color:C.text, fontWeight:700, fontSize:14, flex:1 }}>{meta.label || type}</span>
        <button onClick={onClose} style={iconBtnStyle}><Icons.close /></button>
      </div>

      {/* Метка узла */}
      <FieldBlock label="Метка узла (видна на холсте)">
        <input style={inputStyle} value={label || ""} maxLength={40}
          onChange={e => onChange(config, e.target.value)}
          placeholder="необязательно" />
      </FieldBlock>

      {/* ── ТРИГГЕР ── */}
      {type === "trigger" && (
        <div style={{ color:C.textDim, fontSize:12, lineHeight:1.5 }}>
          Точка входа в сценарий. Настройте триггер в шапке редактора.
        </div>
      )}

      {/* ── СООБЩЕНИЕ ── */}
      {type === "message" && (<>
        <FieldBlock label="Текст сообщения" hint="Поддерживается HTML: <b>, <i>, <code>, <a href=...>. ПКМ по полю — форматирование.">
          <RichTextArea style={taStyle} value={config.text || ""}
            onChange={v => set("text", v)} placeholder="Текст сообщения..." />
        </FieldBlock>
        <FieldBlock label="Фото (file_id или URL)">
          <input style={inputStyle} value={config.photo_file_id || ""}
            onChange={e => set("photo_file_id", e.target.value)}
            placeholder="AgACAgI... или https://..." />
        </FieldBlock>
      </>)}

      {/* ── ВВОД ── */}
      {type === "input" && (<>
        <FieldBlock label="Вопрос пользователю" hint="ПКМ по полю — форматирование. Поддерживается HTML.">
          <RichTextArea style={taStyle} value={config.question || config.prompt || ""}
            onChange={v => set("question", v)} placeholder="Введите ваш вопрос..." />
        </FieldBlock>
        <FieldBlock label="Имя переменной" hint="Латиница, без пробелов. Доступна как {{имя}} в следующих узлах.">
          <input style={inputStyle} value={config.variable_name || ""}
            onChange={e => set("variable_name", e.target.value.replace(/[^a-zA-Z0-9_]/g,""))}
            placeholder="user_answer" />
        </FieldBlock>
      </>)}

      {/* ── УСЛОВИЕ ── */}
      {type === "condition" && (<>
        <FieldBlock label="Переменная" hint="Какую переменную проверять.">
          {allVars.length > 0 ? (
            <select style={inputStyle} value={config.variable || ""}
              onChange={e => set("variable", e.target.value)}>
              <option value="">— выберите —</option>
              {allVars.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
          ) : (
            <input style={inputStyle} value={config.variable || ""}
              onChange={e => set("variable", e.target.value)}
              placeholder="имя_переменной" />
          )}
        </FieldBlock>
        <FieldBlock label="Оператор">
          <select style={inputStyle} value={config.operator || "eq"}
            onChange={e => set("operator", e.target.value)}>
            <option value="eq">= равно</option>
            <option value="neq">≠ не равно</option>
            <option value="contains">содержит</option>
            <option value="gt">&gt; больше</option>
            <option value="lt">&lt; меньше</option>
            <option value="empty">пусто</option>
            <option value="not_empty">не пусто</option>
          </select>
        </FieldBlock>
        <FieldBlock label="Значение">
          <input style={inputStyle} value={config.value || ""}
            onChange={e => set("value", e.target.value)} placeholder="значение" />
        </FieldBlock>
      </>)}

      {/* ── КНОПКИ ── */}
      {type === "buttons" && (<>
        <FieldBlock label="Текст перед кнопками" hint="ПКМ — форматирование.">
          <RichTextArea style={{ ...taStyle, minHeight:60 }} value={config.text || ""}
            onChange={v => set("text", v)} placeholder="Выберите вариант:" />
        </FieldBlock>
        <FieldBlock label="Кнопки" hint="Тип «Ветка» — создаёт выход на холсте. Тип «Ссылка» — открывает URL.">
          <ButtonsEditor
            buttons={config.buttons || []}
            onChange={btns => set("buttons", btns)} />
        </FieldBlock>
      </>)}

      {/* ── ЗАДЕРЖКА ── */}
      {type === "delay" && (
        <FieldBlock label="Задержка (секунды)">
          <input style={inputStyle} type="number" min={1} max={86400}
            value={config.seconds || 5}
            onChange={e => set("seconds", parseInt(e.target.value)||5)} />
        </FieldBlock>
      )}

      {/* ── ЗАДАТЬ ПЕРЕМЕННУЮ ── */}
      {type === "set_variable" && (<>
        <FieldBlock label="Имя переменной">
          {allVars.length > 0 ? (
            <select style={inputStyle} value={config.variable_name || ""}
              onChange={e => set("variable_name", e.target.value)}>
              <option value="">— или введите новое —</option>
              {allVars.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
          ) : null}
          <input style={{ ...inputStyle, marginTop: allVars.length > 0 ? 6 : 0 }}
            value={config.variable_name || ""}
            onChange={e => set("variable_name", e.target.value.replace(/[^a-zA-Z0-9_]/g,""))}
            placeholder="имя_переменной" />
        </FieldBlock>
        <FieldBlock label="Значение">
          <input style={inputStyle} value={config.value || ""}
            onChange={e => set("value", e.target.value)} placeholder="значение" />
        </FieldBlock>
      </>)}

      {/* ── СЛУЧАЙНАЯ ВЕТКА ── */}
      {type === "random" && (
        <FieldBlock label="Ветки (каждая строка = отдельный выход)" hint="Добавьте нужное количество веток.">
          <TagListEditor
            items={config.branches || ["A", "B"]}
            onChange={v => set("branches", v)}
            placeholder="Название ветки"
            addLabel="+ Ветка" />
        </FieldBlock>
      )}

      {/* ── HTTP-ЗАПРОС ── */}
      {type === "http" && (<>
        <FieldBlock label="URL">
          <input style={inputStyle} value={config.url || ""}
            onChange={e => set("url", e.target.value)} placeholder="https://api.example.com/..." />
        </FieldBlock>
        <FieldBlock label="Метод">
          <select style={inputStyle} value={config.method || "GET"}
            onChange={e => set("method", e.target.value)}>
            {["GET","POST","PUT","PATCH","DELETE"].map(m => <option key={m}>{m}</option>)}
          </select>
        </FieldBlock>
        <FieldBlock label="Тело (JSON)">
          <textarea style={{ ...taStyle, minHeight: 60 }}
            value={config.body || ""}
            onChange={e => set("body", e.target.value)}
            placeholder={'{"key": "{{variable}}"}'}
            onKeyDown={e => { if(e.key==="Enter"){ e.stopPropagation(); } }} />
        </FieldBlock>
        <FieldBlock label="Сохранить ответ в переменную">
          <input style={inputStyle} value={config.response_var || ""}
            onChange={e => set("response_var", e.target.value.replace(/[^a-zA-Z0-9_]/g,""))}
            placeholder="response" />
        </FieldBlock>
      </>)}

      {/* ── ОТЧЁТ ── */}
      {type === "report" && (<>
        <FieldBlock label="Переменные для отправки" hint="Будут отправлены в чат администраторов.">
          <VariablePicker
            allVars={allVars}
            selectedVars={config.variables || []}
            onChange={v => set("variables", v)} />
        </FieldBlock>
      </>)}

      {/* ── ВВОД → АДМИН ── */}
      {type === "send_to_admin" && (<>
        <FieldBlock label="Переменная" hint="Значение этой переменной будет отправлено в чат администраторов.">
          {allVars.length > 0 ? (
            <select style={inputStyle} value={config.variable_name || ""}
              onChange={e => set("variable_name", e.target.value)}>
              <option value="">— выберите —</option>
              {allVars.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
          ) : (
            <input style={inputStyle} value={config.variable_name || ""}
              onChange={e => set("variable_name", e.target.value)}
              placeholder="имя_переменной" />
          )}
        </FieldBlock>
        <FieldBlock label="Заголовок сообщения (необязательно)">
          <input style={inputStyle} value={config.title || ""}
            onChange={e => set("title", e.target.value)}
            placeholder="Ответ пользователя:" />
        </FieldBlock>
      </>)}

      {/* ── УВЕДОМИТЬ АДМИНИСТРАТОРОВ ── */}
      {type === "notify_admin" && (
        <FieldBlock label="Текст уведомления" hint="Поддерживает {{переменные}}. ПКМ — форматирование.">
          <RichTextArea style={taStyle} value={config.text || ""}
            onChange={v => set("text", v)}
            placeholder="Пользователь завершил сценарий..." />
        </FieldBlock>
      )}

      {/* ── ОТКРЫТЬ ТИКЕТ ── */}
      {type === "open_ticket" && (
        <FieldBlock label="Тема обращения (необязательно)" hint="Будет видна администраторам в шапке тикета.">
          <input style={inputStyle} value={config.subject || ""}
            onChange={e => set("subject", e.target.value)}
            placeholder="{{topic}} или статичный текст" />
        </FieldBlock>
      )}

      {/* ── ПЕРЕЙТИ К СЦЕНАРИЮ ── */}
      {type === "jump_scenario" && (
        <FieldBlock label="Целевой сценарий" hint="Текущий сценарий завершится и немедленно запустится выбранный.">
          <select style={inputStyle} value={config.scenario_id || ""}
            onChange={e => {
              const opt = e.target.options[e.target.selectedIndex];
              set("scenario_id", e.target.value);
              onChange({ ...config, scenario_id: e.target.value,
                scenario_name: opt.text }, label);
            }}>
            <option value="">— выберите сценарий —</option>
            {scenarios.map(s => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
          {scenarios.length === 0 && (
            <div style={{ color:C.textDim, fontSize:11, marginTop:4 }}>
              Загрузка списка сценариев...
            </div>
          )}
        </FieldBlock>
      )}

      {/* ── КОНЕЦ ── */}
      {type === "end" && (
        <FieldBlock label="Сообщение по завершении (необязательно)">
          <RichTextArea style={{ ...taStyle, minHeight:60 }} value={config.text || ""}
            onChange={v => set("text", v)} placeholder="Спасибо! Сценарий завершён." />
        </FieldBlock>
      )}

      <div style={{ marginTop: 24 }}>
        <button onClick={onDelete} style={{
          ...btnStyle(C.dangerBg, C.danger),
          display: "flex", alignItems: "center", gap: 6,
        }}>
          <Icons.trash /> Удалить узел
        </button>
      </div>
    </div>
  );
}

function FieldBlock({ label, hint, children }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={labelStyle}>{label}</label>
      {hint && <p style={{ color: C.textDim, fontSize: 11, marginBottom: 5, lineHeight: 1.4 }}>{hint}</p>}
      {children}
    </div>
  );
}


// ─── Шапка редактора сценария ─────────────────────────────────────────────────
function ScenarioHeader({ name, setName, triggerType, setTriggerType,
  triggerValue, setTriggerValue, isActive, setIsActive,
  onSave, onBack, saving, isMobile }) {

  const selectedTrigger = TRIGGER_OPTIONS.find(t => t.value === triggerType);

  if (isMobile) {
    return (
      <div style={{
        position: "fixed", top: 0, left: 0, right: 0,
        background: C.surface, borderBottom: `1px solid ${C.border}`,
        padding: "8px 12px", zIndex: 200,
        fontFamily: "'Inter', system-ui, sans-serif",
      }}>
        {/* Строка 1: кнопка назад + название + сохранить */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <button onClick={onBack} style={{ ...iconBtnStyle, flexShrink: 0 }}>
            <Icons.back />
          </button>
          <input style={{ ...inputStyle, flex: 1, margin: 0 }}
            value={name} maxLength={128}
            onChange={e => setName(e.target.value)} placeholder="Название сценария" />
          <button onClick={onSave} disabled={saving}
            style={{ ...btnStyle(C.greenDim, "#fff", saving), flexShrink: 0,
              display: "flex", alignItems: "center", gap: 5 }}>
            {saving ? "…" : <><Icons.save /> Сохранить</>}
          </button>
        </div>

        {/* Строка 2: триггер + значение + активен */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select style={{ ...inputStyle, flex: "0 0 130px", margin: 0 }}
            value={triggerType} onChange={e => { setTriggerType(e.target.value); setTriggerValue(""); }}>
            {TRIGGER_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          {triggerType !== "start" && (
            <input style={{ ...inputStyle, flex: 1, margin: 0 }}
              value={triggerValue} maxLength={256}
              onChange={e => setTriggerValue(e.target.value)}
              placeholder={triggerType === "command" ? "help (без /)" : "значение"} />
          )}
          <label style={{ display: "flex", alignItems: "center", gap: 5,
            color: C.textMid, fontSize: 12, cursor: "pointer", flexShrink: 0 }}>
            <input type="checkbox" checked={isActive} onChange={e => setIsActive(e.target.checked)}
              style={{ accentColor: C.green }} />
            Вкл
          </label>
        </div>

        {/* Подсказка по триггеру */}
        {selectedTrigger && (
          <div style={{ fontSize: 11, color: C.textDim, marginTop: 4 }}>
            {selectedTrigger.hint}
          </div>
        )}
      </div>
    );
  }

  return (
    <div style={{
      position: "fixed", top: 0, left: 0, right: 0, height: 60,
      background: C.surface, borderBottom: `1px solid ${C.border}`,
      display: "flex", alignItems: "center", gap: 10, padding: "0 16px",
      zIndex: 200, fontFamily: "'Inter', system-ui, sans-serif",
    }}>
      <button onClick={onBack} style={{ ...btnStyle(C.elevated, C.textMid),
        display: "flex", alignItems: "center", gap: 6 }}>
        <Icons.back /> Назад
      </button>

      <div style={{ width: 1, height: 28, background: C.border, flexShrink: 0 }} />

      <input style={{ ...inputStyle, width: 200, margin: 0 }}
        value={name} maxLength={128}
        onChange={e => setName(e.target.value)} placeholder="Название сценария" />

      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <select style={{ ...inputStyle, width: 155, margin: 0, fontSize: 12 }}
          value={triggerType} onChange={e => { setTriggerType(e.target.value); setTriggerValue(""); }}>
          {TRIGGER_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {selectedTrigger && (
          <span style={{ fontSize: 10, color: C.textDim, paddingLeft: 2 }}>
            {selectedTrigger.hint}
          </span>
        )}
      </div>

      {triggerType !== "start" && (
        <input style={{ ...inputStyle, width: 155, margin: 0 }}
          value={triggerValue} maxLength={256}
          onChange={e => setTriggerValue(e.target.value)}
          placeholder={triggerType === "command" ? "help (без /)" : "Значение"} />
      )}

      <label style={{ display: "flex", alignItems: "center", gap: 6,
        color: C.textMid, fontSize: 13, cursor: "pointer", marginLeft: "auto",
        userSelect: "none" }}>
        <input type="checkbox" checked={isActive} onChange={e => setIsActive(e.target.checked)}
          style={{ accentColor: C.green, width: 15, height: 15 }} />
        Активен
      </label>

      <button onClick={onSave} disabled={saving}
        style={{ ...btnStyle(C.greenDim, "#fff", saving),
          display: "flex", alignItems: "center", gap: 6 }}>
        {saving ? "Сохранение…" : <><Icons.save /> Сохранить</>}
      </button>
    </div>
  );
}


// ─── Палитра узлов ────────────────────────────────────────────────────────────
function NodePalette({ onAdd, isMobile }) {
  if (isMobile) {
    return (
      <div style={{
        position: "fixed", bottom: 0, left: 0, right: 0,
        background: C.surface, borderTop: `1px solid ${C.border}`,
        padding: "10px 12px", zIndex: 100,
        display: "flex", gap: 6, overflowX: "auto",
        fontFamily: "'Inter', system-ui, sans-serif",
      }}>
        {Object.entries(NODE_TYPES_META).map(([type, meta]) => (
          <button key={type} onClick={() => onAdd(type)}
            style={{
              background: C.elevated,
              border: `1px solid ${meta.color}44`,
              borderRadius: 8, padding: "6px 10px",
              display: "flex", flexDirection: "column", alignItems: "center", gap: 3,
              color: meta.color, fontSize: 10, fontWeight: 600,
              cursor: "pointer", flexShrink: 0, minWidth: 60,
              fontFamily: "inherit",
            }}>
            <span style={{ display: "flex" }}><meta.Icon /></span>
            <span style={{ color: C.textMid, fontSize: 9 }}>{meta.label}</span>
          </button>
        ))}
      </div>
    );
  }

  return (
    <div style={{
      position: "fixed", left: 12, top: 72, bottom: 12,
      width: 154, background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 12, padding: 10, zIndex: 100,
      fontFamily: "'Inter', system-ui, sans-serif",
      display: "flex", flexDirection: "column", gap: 4,
      overflowY: "auto",
    }}>
      <p style={{ color: C.textDim, fontSize: 10, fontWeight: 700,
        letterSpacing: ".06em", textTransform: "uppercase", margin: "0 0 6px" }}>
        Добавить блок
      </p>
      {[
        { label: "Базовые",    items: ["message","input","buttons","delay"] },
        { label: "Логика",     items: ["condition","random","set_variable","http"] },
        { label: "Администр.", items: ["send_to_admin","notify_admin","open_ticket","report"] },
        { label: "Финал",      items: ["jump_scenario","end"] },
      ].map(group => (
        <div key={group.label}>
          <div style={{ color:C.textDim, fontSize:9, fontWeight:700, letterSpacing:".08em",
            textTransform:"uppercase", margin:"8px 0 4px 2px" }}>{group.label}</div>
          {group.items.map(type => {
            const meta = NODE_TYPES_META[type];
            if (!meta) return null;
            return (
              <button key={type} onClick={() => onAdd(type)}
                style={{
                  background: C.elevated, border: `1px solid ${meta.color}33`,
                  borderRadius: 8, padding: "7px 10px", cursor: "pointer",
                  display: "flex", alignItems: "center", gap: 7, textAlign: "left",
                  color: C.text, fontSize: 12, transition: "background .1s",
                  fontFamily: "inherit", width: "100%", marginBottom: 3,
                }}
                title={meta.hint}
                onMouseEnter={e => e.currentTarget.style.background = meta.color + "20"}
                onMouseLeave={e => e.currentTarget.style.background = C.elevated}
              >
                <span style={{ color: meta.color, display: "flex", flexShrink: 0 }}>
                  <meta.Icon />
                </span>
                <span>{meta.label}</span>
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}


// ─── Список сценариев ─────────────────────────────────────────────────────────
function ScenarioList({ botId, onSelect, onNew, initData }) {
  const [scenarios, setScenarios] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api(initData).get(`/bots/${botId}/scenarios`)
      .then(setScenarios).catch(console.error)
      .finally(() => setLoading(false));
  }, [botId, initData]);

  return (
    <div style={{
      minHeight: "100vh", background: C.bg,
      fontFamily: "'Inter', system-ui, sans-serif", padding: 20,
    }}>
      <div style={{ maxWidth: 520, margin: "0 auto" }}>
        {/* Шапка */}
        <div style={{ display: "flex", justifyContent: "space-between",
          alignItems: "center", marginBottom: 24 }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <span style={{ color: C.green, display: "flex" }}><Icons.bolt /></span>
              <h1 style={{ color: C.text, margin: 0, fontSize: 20, fontWeight: 700 }}>
                Сценарии
              </h1>
            </div>
            <p style={{ color: C.textMid, margin: 0, fontSize: 13 }}>
              Автоматические диалоги для вашего бота
            </p>
          </div>
          <button onClick={onNew} style={{ ...btnStyle(C.greenDim, "#fff"),
            display: "flex", alignItems: "center", gap: 6 }}>
            <Icons.plus /> Создать
          </button>
        </div>

        {/* Пояснительная плашка */}
        <div style={{ background: C.elevated, borderRadius: 10, padding: "12px 16px",
          marginBottom: 20, border: `1px solid ${C.border}` }}>
          <p style={{ color: C.textMid, margin: 0, fontSize: 12, lineHeight: 1.6 }}>
            <strong style={{ color: C.text }}>Сценарий</strong> — это последовательность шагов,
            которые бот выполнит автоматически при наступлении события (команды, нажатия кнопки и т.д.).
            Нажмите «Создать», чтобы начать.
          </p>
        </div>

        {loading && (
          <div style={{ textAlign: "center", padding: 40, color: C.textDim }}>
            Загрузка…
          </div>
        )}

        {!loading && scenarios.length === 0 && (
          <div style={{
            background: C.surface, border: `1px dashed ${C.border}`,
            borderRadius: 12, padding: 40, textAlign: "center",
          }}>
            <div style={{ color: C.green, marginBottom: 12, display: "flex",
              justifyContent: "center" }}><Icons.bolt /></div>
            <p style={{ color: C.textMid, margin: 0, fontSize: 14 }}>
              Сценариев пока нет.<br />
              <span style={{ color: C.textDim }}>Нажмите «Создать», чтобы добавить первый.</span>
            </p>
          </div>
        )}

        {scenarios.map(sc => (
          <div key={sc.id} onClick={() => onSelect(sc.id)}
            style={{
              background: C.surface, border: `1px solid ${C.border}`,
              borderRadius: 10, padding: "13px 16px", marginBottom: 8,
              cursor: "pointer", display: "flex", alignItems: "center",
              transition: "border-color .15s",
            }}
            onMouseEnter={e => e.currentTarget.style.borderColor = C.borderHi}
            onMouseLeave={e => e.currentTarget.style.borderColor = C.border}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ color: C.text, fontWeight: 600, fontSize: 14,
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {sc.name}
              </div>
              <div style={{ color: C.textDim, fontSize: 12, marginTop: 2 }}>
                {TRIGGER_OPTIONS.find(t => t.value === sc.trigger_type)?.label}
                {sc.trigger_value ? ` · ${sc.trigger_value}` : ""}
              </div>
            </div>
            <span style={{
              background: sc.is_active ? C.greenBg : C.elevated,
              color: sc.is_active ? C.green : C.textDim,
              borderRadius: 20, padding: "3px 10px", fontSize: 11, fontWeight: 700,
              flexShrink: 0, marginLeft: 12,
            }}>
              {sc.is_active ? "Активен" : "Выкл"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


// ─── Редактор флоу ────────────────────────────────────────────────────────────
const nodeTypes = { flowNode: FlowNode };

let _nodeCounter = 100;
function newId() { return `n${++_nodeCounter}`; }

function makeRFNode(type, position = { x: 200, y: 200 }) {
  return {
    id: newId(),
    type: "flowNode",
    position,
    data: { nodeType: type, config: {}, label: "" },
  };
}

function FlowEditor({ botId, scenarioId, initData, onBack }) {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedNode, setSelectedNode] = useState(null);
  const [name, setName] = useState("Новый сценарий");
  const [triggerType, setTriggerType] = useState("command");
  const [triggerValue, setTriggerValue] = useState("");
  const [isActive, setIsActive] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState(null);
  const [isMobile, setIsMobile] = useState(window.innerWidth < 640);
  const reactFlowWrapper = useRef(null);

  useEffect(() => {
    const handler = () => setIsMobile(window.innerWidth < 640);
    window.addEventListener("resize", handler);
    return () => window.removeEventListener("resize", handler);
  }, []);

  // Высота шапки меняется на мобильных
  const headerH = isMobile ? 86 : 60;
  const bottomBarH = isMobile ? 70 : 0;
  const leftPad = isMobile ? 0 : 166;

  useEffect(() => {
    if (!scenarioId) {
      setNodes([makeRFNode("trigger", { x: 300, y: 80 })]);
      return;
    }
    api(initData).get(`/bots/${botId}/scenarios/${scenarioId}`)
      .then(data => {
        setName(data.name);
        setTriggerType(data.trigger_type);
        setTriggerValue(data.trigger_value || "");
        setIsActive(data.is_active);
        const rfNodes = data.nodes.map(n => ({
          id: String(n.id),
          type: "flowNode",
          position: { x: n.pos_x, y: n.pos_y },
          data: { nodeType: n.node_type, config: n.config, label: n.label || "" },
        }));
        const rfEdges = data.edges.map(e => {
          const cond = e.condition || {};
          return {
            id: `e${e.id}`,
            source: String(e.from_node_id),
            target: String(e.to_node_id),
            sourceHandle: cond.branch || null,
            markerEnd: { type: MarkerType.ArrowClosed, color: C.borderHi },
            style: { stroke: cond.branch === "true" ? C.green
              : cond.branch === "false" ? C.danger : C.borderHi, strokeWidth: 2 },
            label: cond.branch ? (cond.branch === "true" ? "ДА" : "НЕТ") : "",
            labelStyle: { fill: C.textMid, fontSize: 10 },
            data: { condition: cond },
          };
        });
        setNodes(rfNodes);
        setEdges(rfEdges);
      })
      .catch(() => showToast("Ошибка загрузки сценария", "error"));
  }, [scenarioId]);

  const onConnect = useCallback(params => {
    const branch = params.sourceHandle;
    const condition = branch ? { branch } : {};
    const edge = {
      ...params,
      id: `e${Date.now()}`,
      markerEnd: { type: MarkerType.ArrowClosed, color: C.borderHi },
      style: { stroke: branch === "true" ? C.green
        : branch === "false" ? C.danger : C.borderHi, strokeWidth: 2 },
      label: branch ? (branch === "true" ? "ДА" : "НЕТ") : "",
      labelStyle: { fill: C.textMid, fontSize: 10 },
      data: { condition },
    };
    setEdges(eds => addEdge(edge, eds));
  }, []);

  const onNodeClick = useCallback((_, node) => setSelectedNode(node), []);
  const onPaneClick = useCallback(() => setSelectedNode(null), []);

  function addNode(type) {
    const node = makeRFNode(type, { x: 160 + Math.random() * 200, y: 100 + Math.random() * 200 });
    setNodes(nds => [...nds, node]);
  }

  function updateSelectedConfig(newCfg, newLabel) {
    setNodes(nds => nds.map(n => {
      if (n.id !== selectedNode.id) return n;
      return { ...n, data: {
        ...n.data,
        config: typeof newCfg === "object" ? newCfg : n.data.config,
        label: newLabel !== undefined ? newLabel : n.data.label,
      }};
    }));
    setSelectedNode(prev => ({ ...prev, data: {
      ...prev.data,
      config: typeof newCfg === "object" ? newCfg : prev.data.config,
      label: newLabel !== undefined ? newLabel : prev.data.label,
    }}));
  }

  function deleteSelected() {
    if (!selectedNode) return;
    setNodes(nds => nds.filter(n => n.id !== selectedNode.id));
    setEdges(eds => eds.filter(e => e.source !== selectedNode.id && e.target !== selectedNode.id));
    setSelectedNode(null);
  }

  function showToast(msg, type = "success") {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  }

  async function handleSave() {
    setSaving(true);
    try {
      const payload = {
        name,
        trigger_type: triggerType,
        trigger_value: triggerValue || null,
        is_active: isActive,
        nodes: nodes.map(n => ({
          id: n.id,
          node_type: n.data.nodeType,
          config: n.data.config || {},
          pos_x: n.position.x,
          pos_y: n.position.y,
          label: n.data.label || null,
        })),
        edges: edges.map(e => ({
          from_id: e.source,
          to_id: e.target,
          condition: e.data?.condition || {},
        })),
      };
      if (scenarioId) {
        await api(initData).put(`/bots/${botId}/scenarios/${scenarioId}`, payload);
      } else {
        await api(initData).post(`/bots/${botId}/scenarios`, payload);
      }
      showToast("Сценарий сохранён");
    } catch (e) {
      showToast(e.message || "Ошибка сохранения", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ width: "100vw", height: "100vh", background: C.bg }} ref={reactFlowWrapper}>
      <ScenarioHeader
        name={name} setName={setName}
        triggerType={triggerType} setTriggerType={setTriggerType}
        triggerValue={triggerValue} setTriggerValue={setTriggerValue}
        isActive={isActive} setIsActive={setIsActive}
        onSave={handleSave} onBack={onBack} saving={saving}
        isMobile={isMobile}
      />

      <NodePalette onAdd={addNode} isMobile={isMobile} />

      <div style={{ paddingTop: headerH, paddingLeft: leftPad,
        paddingBottom: bottomBarH, height: "100vh" }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          nodeTypes={nodeTypes}
          fitView
          proOptions={{ hideAttribution: true }}
          defaultEdgeOptions={{
            markerEnd: { type: MarkerType.ArrowClosed, color: C.borderHi },
            style: { stroke: C.borderHi, strokeWidth: 2 },
          }}
        >
          <Background color={C.border} gap={24} variant="dots" />
          <Controls style={{
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 8, overflow: "hidden",
            bottom: isMobile ? bottomBarH + 8 : 12,
          }} />
          {!isMobile && (
            <MiniMap
              nodeColor={n => NODE_TYPES_META[n.data?.nodeType]?.color || C.textDim}
              style={{ background: C.surface, border: `1px solid ${C.border}` }}
            />
          )}
        </ReactFlow>
      </div>

      {/* Панель настроек — открывается при клике на узел */}
      {selectedNode && (
        <ConfigPanel
          node={{ ...selectedNode, botId }}
          onChange={updateSelectedConfig}
          onClose={() => setSelectedNode(null)}
          onDelete={deleteSelected}
          isMobile={isMobile}
          allNodes={nodes}
        />
      )}

      {/* Toast-уведомление */}
      {toast && (
        <div style={{
          position: "fixed", bottom: isMobile ? bottomBarH + 12 : 24,
          left: "50%", transform: "translateX(-50%)",
          background: toast.type === "error" ? C.dangerBg : C.greenBg,
          color: toast.type === "error" ? C.danger : C.green,
          border: `1px solid ${toast.type === "error" ? C.danger + "44" : C.green + "44"}`,
          borderRadius: 8, padding: "10px 20px", fontSize: 14, fontWeight: 600,
          zIndex: 1000, boxShadow: "0 4px 24px #0006",
          fontFamily: "'Inter', system-ui, sans-serif",
          whiteSpace: "nowrap",
        }}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}


// ─── Корень приложения ────────────────────────────────────────────────────────
function App() {
  const params = new URLSearchParams(window.location.search);
  const botId = parseInt(params.get("bot_id") || "0");
  const initData = window.Telegram?.WebApp?.initData || "";

  const [view, setView] = useState("list");
  const [editingId, setEditingId] = useState(null);

  if (!botId) {
    return (
      <div style={{ minHeight: "100vh", background: C.bg,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontFamily: "'Inter', system-ui, sans-serif" }}>
        <p style={{ color: C.danger }}>Ошибка: не передан bot_id</p>
      </div>
    );
  }

  if (view === "editor") {
    return (
      <FlowEditor
        botId={botId}
        scenarioId={editingId}
        initData={initData}
        onBack={() => { setView("list"); setEditingId(null); }}
      />
    );
  }

  return (
    <ScenarioList
      botId={botId}
      initData={initData}
      onNew={() => { setEditingId(null); setView("editor"); }}
      onSelect={id => { setEditingId(id); setView("editor"); }}
    />
  );
}

export default App;


// ─── HTTP-клиент ─────────────────────────────────────────────────────────────
function api(initData) {
  const headers = {
    "Content-Type": "application/json",
    "X-Telegram-Init-Data": initData,
  };
  async function request(method, path, body) {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }
  return {
    get: path => request("GET", path),
    post: (path, body) => request("POST", path, body),
    put: (path, body) => request("PUT", path, body),
    delete: path => request("DELETE", path),
  };
}


// ─── Утилиты ─────────────────────────────────────────────────────────────────
function truncate(str, n) {
  return str.length > n ? str.slice(0, n) + "…" : str;
}

const inputStyle = {
  background: "#0b1a14", border: `1px solid #1a3328`,
  borderRadius: 6, color: "#cde8d8", fontSize: 13,
  padding: "7px 10px", width: "100%", boxSizing: "border-box",
  outline: "none", fontFamily: "inherit",
};

const labelStyle = {
  display: "block", color: "#7aaa8e", fontSize: 11,
  fontWeight: 600, marginBottom: 4,
};

const iconBtnStyle = {
  background: "#122010", border: "none", borderRadius: 7,
  padding: "7px 8px", cursor: "pointer",
  color: "#7aaa8e", display: "flex", alignItems: "center", justifyContent: "center",
  transition: "background .15s",
};

function btnStyle(bg, color = "#cde8d8", disabled = false) {
  return {
    background: disabled ? "#122010" : bg,
    color: disabled ? "#3d6650" : color,
    border: "none", borderRadius: 7,
    padding: "8px 14px", cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 13, fontWeight: 600, fontFamily: "inherit",
    transition: "opacity .15s",
    opacity: disabled ? 0.6 : 1,
    whiteSpace: "nowrap",
  };
}
