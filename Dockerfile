FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# data/ contient players.json : à monter sur un volume persistant, sinon la
# liste des profils suivis repart de zéro à chaque redéploiement.
VOLUME ["/app/data"]
ENV DATA_FILE=/app/data/players.json

CMD ["python", "src/bot.py"]
