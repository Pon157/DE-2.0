

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
  getBezierPath,
  MarkerType,
} from "reactflow";


const API_BASE = "https://flow.dialogengine.ru/api";

const NODE_TYPES_META = {
  trigger:   { label: "Триггер",   color: "#6366f1", icon: "⚡" },
  message:   { label: "Сообщение", color: "#10b981", icon: "💬" },
  input:     { label: "Ввод",      color: "#f59e0b", icon: "✏️" },
  condition: { label: "Условие",   color: "#8b5cf6", icon: "◆" },
  delay:     { label: "Задержка",  color: "#6b7280", icon: "⏱" },
  http:      { label: "HTTP",      color: "#ef4444", icon: "🌐" },
  end:       { label: "Конец",     color: "#374151", icon: "■" },
};

const TRIGGER_OPTIONS = [
  { value: "command",  label: "/команда" },
  { value: "button",   label: "Кнопка (текст)" },
  { value: "keyword",  label: "Ключевое слово" },
  { value: "start",    label: "При /start" },
];

const CONDITION_OPERATORS = [
  { value: "eq",       label: "равно" },
  { value: "contains", label: "содержит" },
  { value: "regex",    label: "regex" },
  { value: "gt",       label: ">" },
  { value: "lt",       label: "<" },
];


function FlowNode({ id, data, selected }) {
  const meta = NODE_TYPES_META[data.nodeType] || NODE_TYPES_META.message;
  const hasInput = data.nodeType !== "trigger";
  const hasOutput = data.nodeType !== "end";
  const isBranch = data.nodeType === "condition";

  return (
    <div style={{
      background: selected ? "#1e293b" : "#0f172a",
      border: `2px solid ${selected ? meta.color : "#334155"}`,
      borderRadius: 10,
      minWidth: 180,
      fontFamily: "'Inter', system-ui, sans-serif",
      boxShadow: selected
        ? `0 0 0 3px ${meta.color}33, 0 4px 24px #0005`
        : "0 4px 16px #0004",
      transition: "border-color .15s, box-shadow .15s",
    }}>
      {hasInput && (
        <Handle type="target" position={Position.Top} style={{
          background: "#475569", border: "2px solid #1e293b",
          width: 12, height: 12, top: -7,
        }} />
      )}

      {/* Шапка */}
      <div style={{
        background: meta.color + "22",
        borderBottom: `1px solid ${meta.color}44`,
        padding: "8px 12px",
        borderRadius: "8px 8px 0 0",
        display: "flex", alignItems: "center", gap: 8,
      }}>
        <span style={{ fontSize: 16 }}>{meta.icon}</span>
        <span style={{ color: meta.color, fontSize: 11, fontWeight: 700,
          letterSpacing: ".06em", textTransform: "uppercase" }}>
          {meta.label}
        </span>
      </div>

      {/* Тело */}
      <div style={{ padding: "10px 12px", color: "#cbd5e1", fontSize: 13, lineHeight: 1.4 }}>
        <NodeSummary type={data.nodeType} config={data.config || {}} />
      </div>

      {/* Нижняя метка */}
      {data.label && (
        <div style={{ padding: "0 12px 8px", color: "#64748b", fontSize: 11, fontStyle: "italic" }}>
          {data.label}
        </div>
      )}

      {/* Хэндлы выхода */}
      {hasOutput && !isBranch && (
        <Handle type="source" position={Position.Bottom} style={{
          background: meta.color, border: "2px solid #1e293b",
          width: 12, height: 12, bottom: -7,
        }} />
      )}
      {isBranch && (
        <>
          <Handle type="source" id="true" position={Position.Bottom} style={{
            background: "#10b981", border: "2px solid #1e293b",
            width: 12, height: 12, bottom: -7, left: "35%",
          }} />
          <Handle type="source" id="false" position={Position.Bottom} style={{
            background: "#ef4444", border: "2px solid #1e293b",
            width: 12, height: 12, bottom: -7, left: "65%",
          }} />
        </>
      )}
    </div>
  );
}

function NodeSummary({ type, config }) {
  if (type === "trigger") return <span style={{ color: "#94a3b8" }}>Точка входа</span>;
  if (type === "end") return <span style={{ color: "#94a3b8" }}>Завершить сценарий</span>;
  if (type === "message") return (
    <span style={{ color: "#e2e8f0" }}>
      {config.text ? truncate(config.text, 60) : <em style={{ color: "#475569" }}>Нет текста</em>}
    </span>
  );
  if (type === "input") return (
    <span>
      <span style={{ color: "#fbbf24" }}>→</span>
      <span style={{ color: "#e2e8f0", marginLeft: 4 }}>{config.variable_name || "input"}</span>
    </span>
  );
  if (type === "condition") return (
    <span style={{ color: "#e2e8f0" }}>
      <span style={{ color: "#a78bfa" }}>{config.variable || "?"}</span>
      {" "}{CONDITION_OPERATORS.find(o => o.value === config.operator)?.label || "="}
      {" "}<span style={{ color: "#fbbf24" }}>"{config.value || "?"}"</span>
    </span>
  );
  if (type === "delay") return (
    <span style={{ color: "#9ca3af" }}>{config.seconds || 0} сек</span>
  );
  if (type === "http") return (
    <span style={{ color: "#fca5a5", fontSize: 12 }}>{truncate(config.url || "—", 40)}</span>
  );
  return null;
}


function ConfigPanel({ node, onChange, onClose, onDelete }) {
  if (!node) return null;
  const cfg = node.data.config || {};
  const meta = NODE_TYPES_META[node.data.nodeType];

  const set = (key, val) => onChange({ ...cfg, [key]: val });

  return (
    <div style={{
      position: "fixed", right: 0, top: 0, bottom: 0, width: 320,
      background: "#0f172a", borderLeft: "1px solid #1e293b",
      padding: 20, overflowY: "auto", zIndex: 100,
      fontFamily: "'Inter', system-ui, sans-serif",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 20 }}>{meta.icon}</span>
          <span style={{ color: meta.color, fontWeight: 700, fontSize: 14 }}>{meta.label}</span>
        </div>
        <button onClick={onClose} style={btnStyle("#334155")}>✕</button>
      </div>

      <label style={labelStyle}>Подпись (необязательно)</label>
      <input style={inputStyle} value={node.data.label || ""} maxLength={128}
        onChange={e => onChange(cfg, e.target.value)} placeholder="Произвольная метка" />

      <div style={{ marginTop: 16 }}>
        {node.data.nodeType === "message" && (
          <>
            <label style={labelStyle}>Текст сообщения</label>
            <textarea style={{ ...inputStyle, height: 120, resize: "vertical" }}
              value={cfg.text || ""} maxLength={4000}
              onChange={e => set("text", e.target.value)}
              placeholder="Поддерживается HTML и {{переменная}}" />
            <label style={{ ...labelStyle, marginTop: 12 }}>file_id фото (необязательно)</label>
            <input style={inputStyle} value={cfg.photo_file_id || ""}
              onChange={e => set("photo_file_id", e.target.value)} placeholder="AgAC..." />
          </>
        )}

        {node.data.nodeType === "input" && (
          <>
            <label style={labelStyle}>Имя переменной</label>
            <input style={inputStyle} value={cfg.variable_name || ""} maxLength={64}
              onChange={e => set("variable_name", e.target.value.replace(/\W/g, "_"))}
              placeholder="user_name" />
            <label style={{ ...labelStyle, marginTop: 12 }}>Вопрос пользователю</label>
            <textarea style={{ ...inputStyle, height: 80, resize: "vertical" }}
              value={cfg.prompt || ""} maxLength={1000}
              onChange={e => set("prompt", e.target.value)}
              placeholder="Как вас зовут?" />
          </>
        )}

        {node.data.nodeType === "condition" && (
          <>
            <label style={labelStyle}>Переменная</label>
            <input style={inputStyle} value={cfg.variable || ""} maxLength={64}
              onChange={e => set("variable", e.target.value)}
              placeholder="user_name" />
            <label style={{ ...labelStyle, marginTop: 12 }}>Оператор</label>
            <select style={inputStyle} value={cfg.operator || "eq"}
              onChange={e => set("operator", e.target.value)}>
              {CONDITION_OPERATORS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            <label style={{ ...labelStyle, marginTop: 12 }}>Значение</label>
            <input style={inputStyle} value={cfg.value || ""} maxLength={256}
              onChange={e => set("value", e.target.value)} placeholder="Ожидаемое значение" />
            <p style={{ color: "#475569", fontSize: 11, marginTop: 8 }}>
              Зелёная ручка → ветка TRUE<br />Красная ручка → ветка FALSE
            </p>
          </>
        )}

        {node.data.nodeType === "delay" && (
          <>
            <label style={labelStyle}>Задержка (секунды, макс. 300)</label>
            <input style={inputStyle} type="number" min={0} max={300}
              value={cfg.seconds || 0}
              onChange={e => set("seconds", Math.min(300, Math.max(0, +e.target.value)))} />
          </>
        )}

        {node.data.nodeType === "http" && (
          <>
            <label style={labelStyle}>URL (только https://)</label>
            <input style={inputStyle} value={cfg.url || ""} maxLength={512}
              onChange={e => set("url", e.target.value)} placeholder="https://example.com/webhook" />
            <label style={{ ...labelStyle, marginTop: 12 }}>Метод</label>
            <select style={inputStyle} value={cfg.method || "POST"}
              onChange={e => set("method", e.target.value)}>
              <option value="POST">POST</option>
              <option value="GET">GET</option>
            </select>
            <label style={{ ...labelStyle, marginTop: 12 }}>Тело запроса (шаблон)</label>
            <textarea style={{ ...inputStyle, height: 80, resize: "vertical" }}
              value={cfg.body_template || ""} maxLength={2000}
              onChange={e => set("body_template", e.target.value)}
              placeholder={'{"name": "{{user_name}}"}'} />
            <label style={{ ...labelStyle, marginTop: 12 }}>Сохранить ответ в переменную</label>
            <input style={inputStyle} value={cfg.output_variable || "_http_body"} maxLength={64}
              onChange={e => set("output_variable", e.target.value)} />
            <p style={{ color: "#475569", fontSize: 11, marginTop: 8 }}>
              HTTP-статус → <code style={{ color: "#94a3b8" }}>_http_status</code>
            </p>
          </>
        )}
      </div>

      <div style={{ marginTop: 24, display: "flex", gap: 8 }}>
        <button onClick={onDelete} style={btnStyle("#7f1d1d", "#fca5a5")}>
          Удалить узел
        </button>
      </div>
    </div>
  );
}


function ScenarioHeader({ name, setName, triggerType, setTriggerType,
  triggerValue, setTriggerValue, isActive, setIsActive,
  onSave, onBack, saving }) {

  return (
    <div style={{
      position: "fixed", top: 0, left: 0, right: 0, height: 56,
      background: "#0f172a", borderBottom: "1px solid #1e293b",
      display: "flex", alignItems: "center", gap: 12, padding: "0 16px",
      zIndex: 200, fontFamily: "'Inter', system-ui, sans-serif",
    }}>
      <button onClick={onBack} style={btnStyle("#1e293b", "#94a3b8")}>← Назад</button>

      <input style={{ ...inputStyle, width: 200, margin: 0 }}
        value={name} maxLength={128}
        onChange={e => setName(e.target.value)} placeholder="Название сценария" />

      <select style={{ ...inputStyle, width: 150, margin: 0 }}
        value={triggerType} onChange={e => { setTriggerType(e.target.value); setTriggerValue(""); }}>
        {TRIGGER_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>

      {triggerType !== "start" && (
        <input style={{ ...inputStyle, width: 160, margin: 0 }}
          value={triggerValue} maxLength={256}
          onChange={e => setTriggerValue(e.target.value)}
          placeholder={triggerType === "command" ? "help (без /)" : "Значение"} />
      )}

      <label style={{ display: "flex", alignItems: "center", gap: 6,
        color: "#94a3b8", fontSize: 13, cursor: "pointer", marginLeft: "auto" }}>
        <input type="checkbox" checked={isActive} onChange={e => setIsActive(e.target.checked)}
          style={{ accentColor: "#6366f1" }} />
        Активен
      </label>

      <button onClick={onSave} disabled={saving}
        style={btnStyle("#6366f1", "#fff", saving)}>
        {saving ? "Сохранение…" : "Сохранить"}
      </button>
    </div>
  );
}


function NodePalette({ onAdd }) {
  return (
    <div style={{
      position: "fixed", left: 16, top: 72, bottom: 16,
      width: 160, background: "#0f172a", border: "1px solid #1e293b",
      borderRadius: 12, padding: 12, zIndex: 100,
      fontFamily: "'Inter', system-ui, sans-serif",
      display: "flex", flexDirection: "column", gap: 6,
      overflowY: "auto",
    }}>
      <p style={{ color: "#475569", fontSize: 11, fontWeight: 700,
        letterSpacing: ".06em", textTransform: "uppercase", marginBottom: 4 }}>
        Узлы
      </p>
      {Object.entries(NODE_TYPES_META).map(([type, meta]) => (
        <button key={type} onClick={() => onAdd(type)}
          style={{
            background: "#1e293b", border: `1px solid ${meta.color}44`,
            borderRadius: 8, padding: "8px 10px", cursor: "pointer",
            display: "flex", alignItems: "center", gap: 8, textAlign: "left",
            color: "#cbd5e1", fontSize: 12, transition: "background .1s",
          }}
          onMouseEnter={e => e.currentTarget.style.background = meta.color + "22"}
          onMouseLeave={e => e.currentTarget.style.background = "#1e293b"}
        >
          <span>{meta.icon}</span>
          <span>{meta.label}</span>
        </button>
      ))}
    </div>
  );
}


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
      minHeight: "100vh", background: "#030712",
      fontFamily: "'Inter', system-ui, sans-serif", padding: 24,
    }}>
      <div style={{ maxWidth: 540, margin: "0 auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between",
          alignItems: "center", marginBottom: 24 }}>
          <div>
            <h1 style={{ color: "#f1f5f9", margin: 0, fontSize: 22, fontWeight: 700 }}>
              ⚡ Сценарии
            </h1>
            <p style={{ color: "#475569", margin: "4px 0 0", fontSize: 13 }}>
              Визуальные флоу для бота
            </p>
          </div>
          <button onClick={onNew} style={btnStyle("#6366f1", "#fff")}>+ Создать</button>
        </div>

        {loading && <p style={{ color: "#475569", textAlign: "center" }}>Загрузка…</p>}

        {!loading && scenarios.length === 0 && (
          <div style={{
            background: "#0f172a", border: "1px dashed #1e293b",
            borderRadius: 12, padding: 40, textAlign: "center",
          }}>
            <p style={{ color: "#334155", fontSize: 32, margin: "0 0 12px" }}>⚡</p>
            <p style={{ color: "#475569", margin: 0 }}>Нет сценариев. Создайте первый!</p>
          </div>
        )}

        {scenarios.map(sc => (
          <div key={sc.id} onClick={() => onSelect(sc.id)}
            style={{
              background: "#0f172a", border: "1px solid #1e293b",
              borderRadius: 10, padding: "14px 16px", marginBottom: 10,
              cursor: "pointer", display: "flex", alignItems: "center",
              transition: "border-color .15s",
            }}
            onMouseEnter={e => e.currentTarget.style.borderColor = "#6366f1"}
            onMouseLeave={e => e.currentTarget.style.borderColor = "#1e293b"}
          >
            <div style={{ flex: 1 }}>
              <div style={{ color: "#f1f5f9", fontWeight: 600 }}>{sc.name}</div>
              <div style={{ color: "#475569", fontSize: 12, marginTop: 2 }}>
                {TRIGGER_OPTIONS.find(t => t.value === sc.trigger_type)?.label}
                {sc.trigger_value ? ` · ${sc.trigger_value}` : ""}
              </div>
            </div>
            <span style={{
              background: sc.is_active ? "#14532d" : "#1e293b",
              color: sc.is_active ? "#86efac" : "#475569",
              borderRadius: 20, padding: "2px 10px", fontSize: 11, fontWeight: 700,
            }}>
              {sc.is_active ? "Активен" : "Выкл"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


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
  const reactFlowWrapper = useRef(null);

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
            markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
            style: { stroke: cond.branch === "true" ? "#10b981"
              : cond.branch === "false" ? "#ef4444" : "#475569", strokeWidth: 2 },
            label: cond.branch || "",
            labelStyle: { fill: "#94a3b8", fontSize: 10 },
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
      markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
      style: { stroke: branch === "true" ? "#10b981"
        : branch === "false" ? "#ef4444" : "#475569", strokeWidth: 2 },
      label: branch || "",
      labelStyle: { fill: "#94a3b8", fontSize: 10 },
      data: { condition },
    };
    setEdges(eds => addEdge(edge, eds));
  }, []);

  const onNodeClick = useCallback((_, node) => setSelectedNode(node), []);
  const onPaneClick = useCallback(() => setSelectedNode(null), []);

  function addNode(type) {
    const node = makeRFNode(type, { x: 180 + Math.random() * 200, y: 150 + Math.random() * 200 });
    setNodes(nds => [...nds, node]);
  }

  function updateSelectedConfig(newCfg, newLabel) {
    setNodes(nds => nds.map(n => {
      if (n.id !== selectedNode.id) return n;
      return {
        ...n,
        data: {
          ...n.data,
          config: typeof newCfg === "object" ? newCfg : n.data.config,
          label: newLabel !== undefined ? newLabel : n.data.label,
        },
      };
    }));
    setSelectedNode(prev => ({
      ...prev,
      data: {
        ...prev.data,
        config: typeof newCfg === "object" ? newCfg : prev.data.config,
        label: newLabel !== undefined ? newLabel : prev.data.label,
      },
    }));
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
      showToast("Сценарий сохранён ✓");
    } catch (e) {
      showToast(e.message || "Ошибка сохранения", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ width: "100vw", height: "100vh", background: "#030712" }}
      ref={reactFlowWrapper}>

      <ScenarioHeader
        name={name} setName={setName}
        triggerType={triggerType} setTriggerType={setTriggerType}
        triggerValue={triggerValue} setTriggerValue={setTriggerValue}
        isActive={isActive} setIsActive={setIsActive}
        onSave={handleSave} onBack={onBack} saving={saving}
      />

      <NodePalette onAdd={addNode} />

      <div style={{ paddingTop: 56, paddingLeft: 176, height: "100vh" }}>
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
            markerEnd: { type: MarkerType.ArrowClosed, color: "#475569" },
            style: { stroke: "#475569", strokeWidth: 2 },
          }}
        >
          <Background color="#1e293b" gap={24} variant="dots" />
          <Controls style={{
            background: "#0f172a", border: "1px solid #1e293b",
            borderRadius: 8, overflow: "hidden",
          }} />
          <MiniMap
            nodeColor={n => NODE_TYPES_META[n.data?.nodeType]?.color || "#475569"}
            style={{ background: "#0f172a", border: "1px solid #1e293b" }}
          />
        </ReactFlow>
      </div>

      {selectedNode && (
        <ConfigPanel
          node={selectedNode}
          onChange={updateSelectedConfig}
          onClose={() => setSelectedNode(null)}
          onDelete={deleteSelected}
        />
      )}

      {toast && (
        <div style={{
          position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)",
          background: toast.type === "error" ? "#7f1d1d" : "#14532d",
          color: toast.type === "error" ? "#fca5a5" : "#86efac",
          borderRadius: 8, padding: "10px 20px", fontSize: 14, fontWeight: 600,
          zIndex: 1000, boxShadow: "0 4px 24px #0006",
          fontFamily: "'Inter', system-ui, sans-serif",
        }}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}

// ─── Корень приложения ────────────────────────────────────────────────────────

function App() {
  // В реальном Mini App initData берётся из window.Telegram.WebApp.initData
  // botId — из query-параметра ?bot_id=123 (передаётся при открытии Mini App)
  const params = new URLSearchParams(window.location.search);
  const botId = parseInt(params.get("bot_id") || "0");
  const initData = window.Telegram?.WebApp?.initData || "";

  const [view, setView] = useState("list");   // "list" | "editor"
  const [editingId, setEditingId] = useState(null);

  if (!botId) {
    return (
      <div style={{ minHeight: "100vh", background: "#030712",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontFamily: "'Inter', system-ui, sans-serif" }}>
        <p style={{ color: "#ef4444" }}>Ошибка: не передан bot_id</p>
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


function truncate(str, n) {
  return str.length > n ? str.slice(0, n) + "…" : str;
}

const inputStyle = {
  background: "#1e293b", border: "1px solid #334155",
  borderRadius: 6, color: "#e2e8f0", fontSize: 13,
  padding: "7px 10px", width: "100%", boxSizing: "border-box",
  outline: "none", fontFamily: "inherit",
};

const labelStyle = {
  display: "block", color: "#64748b", fontSize: 11,
  fontWeight: 600, letterSpacing: ".04em",
  textTransform: "uppercase", marginBottom: 4,
};

function btnStyle(bg, color = "#e2e8f0", disabled = false) {
  return {
    background: disabled ? "#1e293b" : bg,
    color: disabled ? "#475569" : color,
    border: "none", borderRadius: 7,
    padding: "8px 14px", cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 13, fontWeight: 600, fontFamily: "inherit",
    transition: "opacity .15s",
    opacity: disabled ? .6 : 1,
    whiteSpace: "nowrap",
  };
}
