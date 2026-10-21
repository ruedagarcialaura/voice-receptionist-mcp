# Build the React dashboard into static files
FROM node:22-slim AS dashboard
WORKDIR /dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# One Python image for both the EHR service and the voice agent 
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY ehr_server/ ehr_server/
COPY agent/ agent/
RUN pip install --no-cache-dir -e .
RUN python -m agent.voice_agent download-files
COPY --from=dashboard /dashboard/dist dashboard/dist
EXPOSE 8000
CMD ["python", "-m", "ehr_server.server", "--http"]
