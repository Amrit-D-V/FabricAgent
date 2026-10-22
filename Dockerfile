FROM python:3.12-slim

# Install system dependencies (curl, ca-certificates, gnupg, build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg2 \
    ca-certificates \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# (Optional) Install Microsoft ODBC Driver 18 for SQL Server
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Install fabricbi CLI package
RUN pip install --no-cache-dir -e . --no-deps

# Create default directories
RUN mkdir -p /data/projects /data/uploads /data/logs
ENV PROJECTS_DIR=/data/projects
ENV UPLOADS_DIR=/data/uploads
ENV FABRICBI_AGENT_HOST=0.0.0.0
ENV FABRICBI_AGENT_PORT=8756
ENV FABRICBI_ALLOW_INSECURE_SHARED=1

EXPOSE 8756

# Default command runs the API server (api.server)
ENTRYPOINT ["fabricbi"]
CMD ["server", "--host", "0.0.0.0", "--port", "8756"]
