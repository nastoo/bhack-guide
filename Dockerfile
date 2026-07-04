FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY core ./core
COPY configs ./configs
COPY services ./services
COPY static ./static
COPY launch.py ./

ENV ROBOT_SIMULATED=true
ENV PORT=8001
EXPOSE 8001

ENTRYPOINT ["python", "-u", "launch.py"]
