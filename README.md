# tmux_tools — AI-to-AI коммуникация через tmux

Набор инструментов для общения между AI-ассистентами (Claude Code, Gemini CLI и др.), работающими в соседних tmux-pane.

---

## Инструменты

### reliable_send — надёжная отправка

Отправляет текст в pane собеседника с верификацией доставки.

**Алгоритм:**
1. Захватывает состояние pane до отправки
2. Отправляет текст через `tmux send-keys` (без Enter)
3. Отправляет Enter отдельной командой
4. Проверяет через `capture-pane`, что текст появился
5. Возвращает `{success: true/false}`

```bash
python3 tmux_tools.py send %0 "Привет, Claude!"
python3 tmux_tools.py send %1 "Привет, Gemini!"
```

```python
from tmux_tools import reliable_send
result = reliable_send("%0", "Привет!")
```

### stream_read — дедуплицированный поток

Захватывает pane и возвращает только **новые** строки с последнего вызова. Дедупликация через хеши строк с контекстом соседей. Вывод автоматически очищается от TUI-мусора.

```bash
python3 tmux_tools.py read %0
python3 tmux_tools.py read %1
```

```python
from tmux_tools import stream_read, tui_to_md
result = stream_read("%1", lines=200)
clean = tui_to_md(result["new_content"])
```

Состояние: `/tmp/ai_chat/.stream_<pane_id>`

### tui_to_md — очистка TUI в Markdown

Преобразует сырой терминальный вывод в чистый текст.

| Что удаляется | Примеры |
|---------------|---------|
| Box-drawing символы | `╭╮╰╯│─` рамки tool outputs |
| ANSI escape-коды | Цвета, курсор |
| Спиннеры | `⠹ Thinking... (5s)`, `✶ Observing...` |
| Статус-бары | `⏵⏵ bypass permissions`, `YOLO mode` |
| UI-подсказки | `Press ↑ to edit`, `ctrl+o to expand` |
| Gemini thought-блоки | `✦ <ctrl46>thought`, `CRITICAL INSTRUCTION` |
| Gemini tool-заголовки | `✓ Shell ...`, `✓ ReadFile ...` |
| Промпты ввода | `* Type your message`, `no sandbox` |

**Принцип:** лучше пропустить мусор, чем потерять полезный контент.

```bash
python3 tmux_tools.py clean raw_capture.txt
tmux capture-pane -pt %1 | python3 tmux_tools.py clean -
```

---

## Известные ограничения

**Ресайз терминала** — при подключении с маленького экрана (телефон) tmux перестраивает pane. Парсер обрабатывает это, но edge cases возможны.

**Idle state** — если AI ждёт ввода, он не получит сообщение автоматически. `reliable_send` доставит текст в поле ввода, но AI увидит его при следующем взаимодействии с пользователем.

**Восклицательный знак** — `!` в начале текста переключает Gemini CLI в shell mode.

---

## Шпаргалка

| Действие | Команда |
|----------|---------|
| Отправить | `python3 tmux_tools.py send %N "текст"` |
| Прочитать (новое) | `python3 tmux_tools.py read %N` |
| Очистить TUI | `python3 tmux_tools.py clean файл` |

---

*Совместно создано Claude (Anthropic) и Gemini (Google) — март 2026.*
