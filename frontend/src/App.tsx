import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

type Theme = "light" | "dark";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
};

type SourceChunk = {
  assignment_title: string;
  topic?: string | null;
  source?: string | null;
  chunk_number?: number | null;
  content: string;
  score: number;
};

type ChatResponse = {
  answer: string;
  sources: SourceChunk[];
};

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const CHUNK_LIMIT = Number(import.meta.env.VITE_CHUNK_LIMIT ?? 6);

const createId = () => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2));

const defaultMessage: ChatMessage[] = [
  {
    id: createId(),
    role: "assistant",
    text: "Привет! Я ассистент, который работает поверх локальной RAG-инфры. Задай вопрос по учебным материалам, а я покажу, что нашлось.",
  },
];

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(defaultMessage);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceChunk[]>([]);
  const [examples, setExamples] = useState<string[]>([]);
  const [theme, setTheme] = useState<Theme>(() => (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/api/examples`, { signal: controller.signal })
      .then((res) => res.json())
      .then((payload) => setExamples(payload.examples ?? []))
      .catch(() => {
        /* ignore sample prompt errors */
      });
    return () => controller.abort();
  }, []);

  const toggleTheme = () => {
    setTheme((current) => (current === "light" ? "dark" : "light"));
  };

  const hasMessages = useMemo(() => messages.length > 0, [messages]);

  const sendMessage = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!input.trim() || loading) {
      return;
    }

    const userMessage: ChatMessage = { id: createId(), role: "user", text: input.trim() };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setError(null);
    setLoading(true);

    try {
      const response = await fetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: userMessage.text, limit: CHUNK_LIMIT }),
      });

      if (!response.ok) {
        const message = await response.json().catch(() => ({}));
        throw new Error(message.detail ?? "Не удалось получить ответ");
      }

      const payload = (await response.json()) as ChatResponse;
      const assistantMessage: ChatMessage = { id: createId(), role: "assistant", text: payload.answer };
      setMessages((prev) => [...prev, assistantMessage]);
      setSources(payload.sources);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Сервис временно недоступен");
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  };

  const handleExampleClick = (value: string) => {
    setInput(value);
    inputRef.current?.focus();
  };

  return (
    <div className={`app theme-${theme}`}>
      <header className="app__header">
        <div>
          <h1>RAG Learning Assistant</h1>
          <p>Мини-чат поверх локальной Qdrant + Postgres связки.</p>
        </div>
        <button className="ghost-button" type="button" onClick={toggleTheme}>
          {theme === "light" ? "🌙 Ночь" : "☀️ День"}
        </button>
      </header>

      <div className="app__body">
        <section className="chat">
          <div className="chat__messages">
            {!hasMessages && <p className="chat__placeholder">Напиши вопрос про задания курса, и я подберу релевантные кусочки.</p>}
            {messages.map((message) => (
              <div key={message.id} className={`chat__bubble chat__bubble--${message.role}`}>
                <span className="chat__role">{message.role === "user" ? "Ты" : "Ассистент"}</span>
                {message.role === "assistant" ? (
                  <ReactMarkdown className="markdown">{message.text}</ReactMarkdown>
                ) : (
                  <p>{message.text}</p>
                )}
              </div>
            ))}
            {loading && <div className="chat__typing">Ассистент печатает…</div>}
          </div>

          <form className="chat__composer" onSubmit={sendMessage}>
            <div className="chat__examples">
              <span>Примеры вопросов:</span>
              <div className="chat__example-buttons">
                {(examples.length ? examples : ["Как построить пайплайн RAG для курса?"]).map((example) => (
                  <button key={example} type="button" className="ghost-button ghost-button--small" onClick={() => handleExampleClick(example)}>
                    {example}
                  </button>
                ))}
              </div>
            </div>
            <div className="chat__input">
              <textarea
                ref={inputRef}
                placeholder="Спроси, как ассистент может помочь…"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={handleKeyDown}
                rows={2}
              />
              <button type="submit" className="primary-button" disabled={loading}>
                {loading ? "Отправка…" : "Отправить"}
              </button>
            </div>
            <small className="chat__hint">Ассистент подтягивает до {CHUNK_LIMIT} фрагментов из векторной базы для ответа.</small>
            {error && <p className="chat__error">{error}</p>}
          </form>
        </section>

        <aside className="context">
          <h2>Контекст</h2>
          <p>Каждый ответ собирается из найденных кусков векторного поиска.</p>
          <div className="context__list">
            {sources.length === 0 && <p>Пока нет подобранных материалов. Задай вопрос, чтобы увидеть фрагменты.</p>}
            {sources.map((source) => (
              <article key={`${source.assignment_title}-${source.chunk_number}`} className="context__card">
                <div className="context__meta">
                  <strong>{source.assignment_title}</strong>
                  {source.topic && <span className="context__topic">{source.topic}</span>}
                </div>
                {source.source && <small className="context__source">Источник: {source.source}</small>}
                <p className="context__excerpt">{source.content}</p>
              </article>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

export default App;
