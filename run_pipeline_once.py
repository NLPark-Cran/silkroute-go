"""One-off runner: execute the pipeline for a product without login (for seeding demo assets)."""
import os, sys, uuid, asyncio

ROOT = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

from app import db, pipeline, seed

db.init()
seed.seed(os.path.join(ROOT, "task_data", "Task_Data", "Data_for_Users(2)"))

product_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
market = sys.argv[2] if len(sys.argv) > 2 else "us"
job_id = uuid.uuid4().hex[:12]
db.job_new(job_id, 0, product_id, market, pipeline.STEPS_TEMPLATE)
asyncio.run(pipeline.run_pipeline(job_id, product_id, market))
j = db.job_get(job_id)
print("STATUS:", j["status"], "| slug:", j["listing_slug"], "| err:", j["error"])
