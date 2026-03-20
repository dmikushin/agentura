FROM archlinux:base

RUN pacman -Syu --noconfirm python python-pip tmux && \
    pip install --break-system-packages aiohttp cryptography && \
    pacman -Scc --noconfirm

WORKDIR /app
COPY tmux_tools.py server.py auth.py ./
COPY .claude/commands/ /app/skills/

RUN mkdir -p /data/streams && chown -R 1000:1000 /data

EXPOSE 7850
ENTRYPOINT ["python3", "-u", "server.py"]
