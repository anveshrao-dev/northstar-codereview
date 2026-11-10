"""Bounded read-only UI index; existing loader remains the authority for reports."""
import os
import stat
from dataclasses import dataclass
from itertools import islice
from datetime import datetime, timezone
from uuid import UUID

from . import real_review as service
from .real_source_contracts import SampleId


@dataclass(frozen=True)
class RetainedReview:
    review: service.RealReviewRun
    file_modified: datetime


def retained_reviews():
    """Never trust directory names, mtime, or an index as report authorization."""
    candidates=[]
    for sample in SampleId:
        try: choices=service.snapshot_catalog(sample)
        except Exception: continue
        for choice in choices[:12]:
            if not choice.usable: continue
            try:
                with service._area(choice.import_id,'review_artifacts') as folder:
                    with os.scandir(folder) as entries:
                        names=[entry.name for entry in islice(entries,4097)]
                    if len(names)>4096: continue
                    for name in names:
                        if not name.endswith('.json'): continue
                        try:
                            run_id=UUID(name[:-5])
                            if str(run_id)+'.json'!=name: continue
                            info=os.stat(name,dir_fd=folder,follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=service.MAX_RECORD: continue
                            candidates.append((info.st_mtime_ns,choice.import_id,run_id,info.st_size))
                        except (OSError,ValueError): continue
            except Exception: continue
    results=[];consumed=0
    for modified,import_id,run_id,size in sorted(candidates,key=lambda r:(r[0],str(r[2])),reverse=True)[:32]:
        if consumed+size>16*1024*1024: break
        consumed+=size
        try:
            review=service.load_review(import_id,run_id)
            results.append(RetainedReview(review,datetime.fromtimestamp(modified/1e9,timezone.utc)))
        except Exception: continue
    return tuple(results)
