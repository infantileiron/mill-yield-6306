const pages=['batches','merges','inspections','diagnosis','reports'];
export function route(){const page=location.hash.slice(1)||'batches';return pages.includes(page)?page:'batches';}
