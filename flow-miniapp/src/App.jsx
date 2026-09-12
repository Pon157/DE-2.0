// flow-miniapp/src/App.jsx
import { useState, useCallback, useEffect, useRef } from "react";
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
  end:          { label: "Конец",          color: "#374151", Icon: Icons.end,          hint: "Завершает сценарий." },
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
      {isRandom && (
        <>
          <Handle type="source" id="a" position={Position.Bottom} style={{
            background: "#fb923c", border: `2px solid ${C.surface}`,
            width: 12, height: 12, bottom: -7, left: "30%",
          }} />
          <Handle type="source" id="b" position={Position.Bottom} style={{
            background: "#a78bfa", border: `2px solid ${C.surface}`,
            width: 12, height: 12, bottom: -7, left: "70%",
          }} />
          <div style={{ display: "flex", justifyContent: "space-between",
            padding: "4px 10px 8px", fontSize: 10 }}>
            <span style={{ color: "#fb923c" }}>A</span>
            <span style={{ color: "#a78bfa" }}>B</span>
          </div>
        </>
      )}
      {isButtons && data.config?.buttons?.length > 0 && (
        <div style={{ padding: "0 10px 10px", display: "flex", flexWrap: "wrap", gap: 4 }}>
          {data.config.buttons.map((btn, i) => (
            <div key={i} style={{
              background: "#a855f722", border: "1px solid #a855f744",
              borderRadius: 4, padding: "2px 8px", fontSize: 10, color: "#d8b4fe",
              position: "relative",
            }}>
              {btn}
              <Handle type="source" id={`btn_${i}`} position={Position.Bottom} style={{
                background: "#a855f7", border: `2px solid ${C.surface}`,
                width: 8, height: 8, bottom: -5, left: "50%",
              }} />
            </div>
          ))}
        </div>
      )}
      {isButtons && (!data.config?.buttons?.length) && (
        <Handle type="source" position={Position.Bottom} style={{
          background: "#a855f7", border: `2px solid ${C.surface}`,
          width: 12, height: 12, bottom: -7,
        }} />
      )}
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
    <span>
      <span style={{ color: C.warn }}>→ </span>
      <span style={{ color: C.text }}>{config.variable_name || "переменная"}</span>
    </span>
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
  return null;
}


// ─── Панель настроек узла (боковая) ──────────────────────────────────────────
function ConfigPanel({ node, onChange, onClose, onDelete, isMobile }) {
  if (!node) return null;
  const cfg = node.data.config || {};
  const meta = NODE_TYPES_META[node.data.nodeType] || NODE_TYPES_META.message;
  const set = (key, val) => onChange({ ...cfg, [key]: val });

  const panelStyle = isMobile ? {
    position: "fixed", left: 0, right: 0, bottom: 0,
    maxHeight: "70vh", borderRadius: "16px 16px 0 0",
    background: C.surface, borderTop: `2px solid ${meta.color}`,
    padding: "16px 16px 24px", overflowY: "auto", zIndex: 300,
    fontFamily: "'Inter', system-ui, sans-serif",
  } : {
    position: "fixed", right: 0, top: 0, bottom: 0, width: 300,
    background: C.surface, borderLeft: `1px solid ${C.border}`,
    padding: 20, overflowY: "auto", zIndex: 200,
    fontFamily: "'Inter', system-ui, sans-serif",
  };

  return (
    <div style={panelStyle}>
      {/* Шапка */}
      <div style={{ display: "flex", alignItems: "center",
        justifyContent: "space-between", marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: meta.color, display: "flex" }}><meta.Icon /></span>
          <span style={{ color: meta.color, fontWeight: 700, fontSize: 15 }}>{meta.label}</span>
        </div>
        <button onClick={onClose} style={iconBtnStyle}><Icons.close /></button>
      </div>

      {/* Подсказка */}
      <div style={{ background: C.elevated, borderRadius: 8, padding: "8px 12px",
        color: C.textMid, fontSize: 12, marginBottom: 16, lineHeight: 1.5 }}>
        {meta.hint}
      </div>

      {/* Метка */}
      <FieldBlock label="Подпись (необязательно)">
        <input style={inputStyle} value={node.data.label || ""} maxLength={128}
          onChange={e => onChange(cfg, e.target.value)} placeholder="Произвольная метка на узле" />
      </FieldBlock>

      {/* Поля по типу */}
      {node.data.nodeType === "message" && (
        <>
          <FieldBlock label="Текст сообщения" hint="Поддерживается HTML: <b>жирный</b>, <i>курсив</i>, а также {{переменная}}">
            <textarea style={{ ...inputStyle, height: 110, resize: "vertical" }}
              value={cfg.text || ""} maxLength={4000}
              onChange={e => set("text", e.target.value)}
              placeholder="Введите текст сообщения..." />
          </FieldBlock>
          <FieldBlock label="Фото (необязательно)" hint="Прямая ссылка на фото (jpg/png) или Telegram file_id">
            <input style={inputStyle} value={cfg.photo_file_id || ""}
              onChange={e => set("photo_file_id", e.target.value)}
              placeholder="https://example.com/photo.jpg  или  AgAC..." />
          </FieldBlock>
        </>
      )}

      {node.data.nodeType === "input" && (
        <>
          <FieldBlock label="Имя переменной" hint="Ответ пользователя сохранится в эту переменную. Используйте только буквы и _">
            <input style={inputStyle} value={cfg.variable_name || ""} maxLength={64}
              onChange={e => set("variable_name", e.target.value.replace(/\W/g, "_"))}
              placeholder="например: user_name" />
          </FieldBlock>
          <FieldBlock label="Вопрос пользователю">
            <textarea style={{ ...inputStyle, height: 80, resize: "vertical" }}
              value={cfg.prompt || ""} maxLength={1000}
              onChange={e => set("prompt", e.target.value)}
              placeholder="Как вас зовут?" />
          </FieldBlock>
        </>
      )}

      {node.data.nodeType === "condition" && (
        <>
          <FieldBlock label="Переменная" hint="Имя переменной, которую нужно проверить">
            <input style={inputStyle} value={cfg.variable || ""} maxLength={64}
              onChange={e => set("variable", e.target.value)} placeholder="user_name" />
          </FieldBlock>
          <FieldBlock label="Условие проверки">
            <select style={inputStyle} value={cfg.operator || "eq"}
              onChange={e => set("operator", e.target.value)}>
              {CONDITION_OPERATORS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </FieldBlock>
          <FieldBlock label="Ожидаемое значение">
            <input style={inputStyle} value={cfg.value || ""} maxLength={256}
              onChange={e => set("value", e.target.value)} placeholder="Иван" />
          </FieldBlock>
          <div style={{ background: C.elevated, borderRadius: 8, padding: "8px 12px",
            fontSize: 12, color: C.textMid, marginTop: 4 }}>
            <span style={{ color: C.green }}>●</span> Зелёная точка → ветка ДА<br />
            <span style={{ color: C.danger }}>●</span> Красная точка → ветка НЕТ
          </div>
        </>
      )}

      {node.data.nodeType === "delay" && (
        <FieldBlock label="Задержка в секундах (не более 300)">
          <input style={inputStyle} type="number" min={0} max={300}
            value={cfg.seconds || 0}
            onChange={e => set("seconds", Math.min(300, Math.max(0, +e.target.value)))} />
        </FieldBlock>
      )}

      {node.data.nodeType === "set_variable" && (
        <>
          <FieldBlock label="Имя переменной" hint="Только буквы, цифры и _">
            <input style={inputStyle} value={cfg.variable_name || ""} maxLength={64}
              onChange={e => set("variable_name", e.target.value.replace(/\W/g, "_"))}
              placeholder="например: score" />
          </FieldBlock>
          <FieldBlock label="Значение">
            <input style={inputStyle} value={cfg.value || ""} maxLength={512}
              onChange={e => set("value", e.target.value)}
              placeholder="42" />
          </FieldBlock>
        </>
      )}

      {node.data.nodeType === "random_branch" && (
        <div style={{ background: C.elevated, borderRadius: 8, padding: "10px 12px",
          fontSize: 12, color: C.textMid, lineHeight: 1.7 }}>
          Случайно выбирает одну из двух веток с вероятностью 50/50.<br/>
          <span style={{ color: "#fb923c" }}>●</span> Левый выход → ветка A<br/>
          <span style={{ color: "#a78bfa" }}>●</span> Правый выход → ветка B
        </div>
      )}

      {node.data.nodeType === "buttons" && (
        <>
          <FieldBlock label="Текст сообщения">
            <textarea style={{ ...inputStyle, height: 80, resize: "vertical" }}
              value={cfg.text || ""} maxLength={1000}
              onChange={e => set("text", e.target.value)}
              placeholder="Выберите вариант:" />
          </FieldBlock>
          <FieldBlock label="Кнопки" hint="Каждая строка — одна кнопка. Текст кнопки = метка ветки.">
            <textarea style={{ ...inputStyle, height: 100, resize: "vertical" }}
              value={(cfg.buttons || []).join("\n")}
              onChange={e => set("buttons", e.target.value.split("\n").map(s => s.trim()).filter(Boolean))}
              placeholder={"Вариант А\nВариант Б\nВариант В"} />
          </FieldBlock>
          <div style={{ background: C.elevated, borderRadius: 8, padding: "8px 12px",
            fontSize: 12, color: C.textMid, marginTop: 4, lineHeight: 1.6 }}>
            Каждая кнопка создаёт отдельный выход из узла.<br/>
            Соедините выходы со следующими шагами.
          </div>
        </>
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
      {Object.entries(NODE_TYPES_META).map(([type, meta]) => (
        <button key={type} onClick={() => onAdd(type)}
          style={{
            background: C.elevated, border: `1px solid ${meta.color}33`,
            borderRadius: 8, padding: "7px 10px", cursor: "pointer",
            display: "flex", alignItems: "center", gap: 7, textAlign: "left",
            color: C.text, fontSize: 12, transition: "background .1s",
            fontFamily: "inherit",
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
          node={selectedNode}
          onChange={updateSelectedConfig}
          onClose={() => setSelectedNode(null)}
          onDelete={deleteSelected}
          isMobile={isMobile}
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
