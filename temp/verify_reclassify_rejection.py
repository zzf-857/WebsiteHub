import asyncio
import sys
from pathlib import Path

api_src = Path("F:/AI/AgentMake/AgentProjects/WebHub/services/api/src").resolve()
sys.path.insert(0, str(api_src))

from webhub.config import get_settings
from webhub.db.database import Database
from webhub.library.reclassify import propose_reclassification


async def main():
    settings = get_settings()
    db = Database(settings.database_url)
    async with db.sessions() as session:
        # Mock non-existent user
        fake_user_id = "non-existent-user-id"
        
        # Test propose_reclassification for unconfigured account
        proposal = await propose_reclassification(session, fake_user_id)
        print("[TEST UNCONFIGURED RESULT]:")
        print(proposal)

    await db.dispose()

if __name__ == "__main__":
    asyncio.run(main())
