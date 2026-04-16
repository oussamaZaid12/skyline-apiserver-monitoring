from pydantic import BaseModel

class InstanceMetricsResponse(BaseModel):
    instance_id: str
    instance_name: str
    instance_status: str
    cpu_percent: float
    memory_mb: float
    memory_bytes: float
    disk_read_bytes_per_sec: float
    disk_write_bytes_per_sec: float
    network_rx_bytes_per_sec: float
    network_tx_bytes_per_sec: float
