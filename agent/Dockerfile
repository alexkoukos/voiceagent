FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Bake the voice-activity and turn-detection models into the image.
RUN python agent.py download-files
CMD ["python", "agent.py", "start"]
