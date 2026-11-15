"""Deactivates jobs no longer seen on the ATS. Run after ats_scraper.py."""
from dotenv import load_dotenv
load_dotenv()
import os
from supabase import create_client

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
supabase.rpc("expire_stale_jobs", {}).execute()
print("Expired stale jobs.")
