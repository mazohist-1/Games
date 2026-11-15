FROM python:3.12-slim
WORKDIR /app
COPY index.html server.py ./
COPY assets ./assets
COPY games ./games
ENV GAMES_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
CMD ["python3", "server.py", "8000"]
