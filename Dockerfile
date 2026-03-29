FROM archlinux:base

RUN pacman-key --init && pacman-key --populate archlinux && \
    pacman -Sy --noconfirm archlinux-keyring && \
    pacman -Syu --noconfirm python python-pip tmux && \
    pip install --break-system-packages aiohttp cryptography \
        ogham-mcp[postgres] ollama && \
    pacman -Scc --noconfirm

WORKDIR /app
COPY server.py auth.py ogham_board.py ./
COPY .claude/commands/ /app/skills/
COPY .claude/CLAUDE.md /app/context/CLAUDE.md
COPY .gemini/GEMINI.md /app/context/GEMINI.md

# Implementation binaries served via /bin/ endpoint.
# Mounted as volume from host (not COPY'd) so updates don't require rebuild.
# Build with: make impl-linux-amd64 impl-linux-arm64
RUN mkdir -p /app/bin

RUN mkdir -p /data/streams && chown -R 1000:1000 /data

EXPOSE 7850
ENTRYPOINT ["python3", "-u", "server.py"]
