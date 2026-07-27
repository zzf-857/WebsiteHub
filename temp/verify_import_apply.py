import asyncio
import sys
from pathlib import Path

api_src = Path("F:/AI/AgentMake/AgentProjects/WebHub/services/api/src").resolve()
sys.path.insert(0, str(api_src))

from sqlalchemy import select
from webhub.config import get_settings
from webhub.db.database import Database
from webhub.db.models import User
from webhub.db.models import BookmarkImportJob, User

from webhub.bookmarks import queries

async def main():
    settings = get_settings()
    db = Database(settings.database_url)
    async with db.sessions() as session:
        user = (await session.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
        if not user:
            print("[ERROR] admin user not found!")
            return

        # Check existing jobs for admin
        stmt = select(BookmarkImportJob).where(BookmarkImportJob.user_id == user.id).order_by(BookmarkImportJob.created_at.desc())
        jobs = list((await session.execute(stmt)).scalars().all())
        print(f"[FOUND JOBS]: {len(jobs)} jobs for admin")
        for job in jobs[:5]:
            print(f"  Job ID: {job.id}, State: {job.state}, Version: {job.version}")

        if jobs:
            latest_job = jobs[0]
            if latest_job.state in {"parse_preview_ready", "final_preview_ready"}:
                print(f"[TESTING APPLY] Job ID: {latest_job.id}, Version: {latest_job.version}")
                try:
                    res = await queries.apply_import(session, user.id, latest_job.id, expected_job_version=latest_job.version)
                    print("[APPLY SUCCESS]:", res)
                except Exception as e:
                    import traceback
                    print("[APPLY EXCEPTION STACKTRACE]:")
                    traceback.print_exc()

    await db.dispose()

if __name__ == "__main__":
    asyncio.run(main())
