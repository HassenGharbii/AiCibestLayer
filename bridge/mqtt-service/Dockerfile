# bullseye, not the (now default) bookworm: bookworm's glibc uses clone3()
# for thread creation, which the Jetson's old L4T kernel (~4.9) doesn't
# support. Docker >=20.10.9 has a seccomp rule that blocks clone3 so glibc
# falls back to clone() cleanly — this Jetson is on 20.10.7, just under that
# fix, so clone3 reaches the kernel and fails hard ("can't start new
# thread") instead of falling back. bullseye's older glibc never tries
# clone3 in the first place, sidestepping the issue entirely.
FROM python:3.11-slim-bullseye

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

COPY app.py init.sql ./

EXPOSE 8001

CMD ["python", "app.py"]
