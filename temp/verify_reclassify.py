import asyncio
import sys
from pathlib import Path

# Add services/api/src to python path
api_src = Path("F:/AI/AgentMake/AgentProjects/WebHub/services/api/src").resolve()
sys.path.insert(0, str(api_src))

from webhub.config import get_settings
from webhub.db.database import Database
from webhub.library.reclassify import propose_reclassification


async def main():
    settings = get_settings()
    db = Database(settings.database_url)
    async with db.sessions() as session:
        # Find admin user id
        from sqlalchemy import select
        from webhub.db.models import User
        user = (await session.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
        if not user:
            print("[ERROR] admin user not found in database!")
            return
        
        print(f"[SUCCESS] Loaded user: {user.username} (id: {user.id})")
        
        # Test propose_reclassification
        proposal = await propose_reclassification(session, user.id)
        print("[TEST PROPOSAL RESULT]:")
        print(proposal)

    await db.dispose()

if __name__ == "__main__":
    asyncio.run(main())
