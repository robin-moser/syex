import os
import sys
import aiohttp
import asyncio

from synology_dsm import SynologyDSM, exceptions
from synology_dsm.helpers import SynoFormatHelper

from prometheus_client import start_http_server, Gauge, Info, Enum
from time import sleep


prometheus_prefix = "syno"

volume_states = ["normal","attention","error","scrubbing"]
smart_states = ["normal","error"]
disk_states = ["normal","error"]

def require_environmental_variable(variable_name):
    if variable_name not in os.environ.keys():
        print('Variable {} missing.'.format(variable_name))
        sys.exit(1)
    return os.environ[variable_name]


def metric(_name):
    return "{}_{}".format(prometheus_prefix, _name)


def set_metadata(api: SynologyDSM, metadata_info):
    metadata_info.info({
        "model": api.information.model,
        "amount_of_ram": str(api.information.ram),
        "serial_number": api.information.serial,
        "dsm_version": api.information.version_string
    })

def set_usage(api: SynologyDSM, temp_gauge, uptime_gauge, cpu_gauge):
    cpu_gauge.set(api.utilisation.cpu_total_load)
    temp_gauge.set(api.information.temperature)
    uptime_gauge.set(api.information.uptime)

def set_memory(api: SynologyDSM, memory_used_gauge, memory_total_gauge):
    memory_use_percentage = int(api.utilisation.memory_real_usage)
    memory_total = int(api.utilisation.memory_size(human_readable=False))
    memory_total_used = (memory_use_percentage / 100) * memory_total

    memory_used_gauge.set(memory_total_used)
    memory_total_gauge.set(memory_total)


def set_network(api: SynologyDSM, network_up_gauge, network_down_gauge):
    network_up = api.utilisation.network_up(human_readable=False)
    network_down = api.utilisation.network_down(human_readable=False)

    if network_up:
        network_up_gauge.set(int(network_up))
    if network_down:
        network_down_gauge.set(int(network_down))

def set_volumes(api: SynologyDSM, volume_status_enum, volume_size_gauge, volume_size_used_gauge):
    for volume_id in api.storage.volumes_ids:
        volume_status = str(api.storage.volume_status(volume_id))
        if volume_status and volume_status.endswith("scrubbing"):
            volume_status = "scrubbing"
        if volume_status not in volume_states:
            volume_status = "error"

        volume_status_enum.labels(volume_id).state(volume_status)

        volume_size_used = str(api.storage.volume_size_used(volume_id, human_readable=False))
        volume_size_used_gauge.labels(volume_id).set(volume_size_used)

        volume_size_total = str(api.storage.volume_size_total(volume_id, human_readable=False))
        volume_size_gauge.labels(volume_id).set(volume_size_total)


def set_disks(api: SynologyDSM, smart_status_enum, disk_status_enum, disk_temp_gauge):
    for disk in api.storage.disks:
        disk_id = disk.get("id")
        disk_name = disk.get("name")
        disk_model = disk.get("model")

        smart_status = disk.get("smart_status")
        smart_status = smart_status if smart_status in smart_states else "error"
        smart_status_enum.labels(disk_id, disk_name, disk_model).state(smart_status)

        disk_status = disk.get("status")
        disk_status = disk_status if disk_status in disk_states else "error"
        disk_status_enum.labels(disk_id, disk_name, disk_model).state(disk_status)

        disk_temp = disk.get("temp")
        disk_temp_gauge.labels(disk_id, disk_name, disk_model).set(disk_temp)


def set_shares(api: SynologyDSM, share_size_used_gauge, share_size_quota_gauge):
    for share in api.share.shares:
        # external drives are not supported, so exclude them
        if share.get("share_quota_used"):
            size = SynoFormatHelper.megabytes_to_bytes(share.get("share_quota_used", 0))
            quota = SynoFormatHelper.megabytes_to_bytes(share.get("quota_value", 0))

            share_size_used_gauge.labels(share.get("uuid"), share.get("name")).set(size)
            share_size_quota_gauge.labels(share.get("uuid"), share.get("name")).set(quota)

async def main():
    verify = os.getenv('SYNOLOGY_VERIFY_SSL', 'false').lower() in ('true', '1')
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=verify)
    ) as session:
        await do(session)


async def do(session: aiohttp.ClientSession):
    url = require_environmental_variable('SYNOLOGY_URL')
    port = require_environmental_variable('SYNOLOGY_PORT')
    user = require_environmental_variable('SYNOLOGY_USER')
    password = require_environmental_variable('SYNOLOGY_PASSWORD')
    https = os.getenv('SYNOLOGY_HTTPS', 'false').lower() in ('true', '1')
    frequency = int(os.getenv('FREQUENCY', 15))

    api: SynologyDSM = SynologyDSM(session, url, int(port), user, password, use_https=https, timeout=15 )

    try:
        await api.login()
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    start_http_server(9999)

    metadata_info = Info(metric("model_metadata"), "Model metadata")

    cpu_gauge = Gauge(metric("cpu_load"), "DSM version")
    temp_gauge = Gauge(metric("temperature"), "Temperature")
    uptime_gauge = Gauge(metric("uptime"), "Uptime")

    memory_used_gauge = Gauge(metric("memory_used"), "Total memory used")
    memory_total_gauge = Gauge(metric("memory_total"), "Total memory")

    network_up_gauge = Gauge(metric("network_up"), "Network up")
    network_down_gauge = Gauge(metric("network_down"), "Network down")

    volume_labels = ["Volume_ID"]
    volume_status_enum = Enum(metric("volume_status"), "Status of volume", volume_labels, states=volume_states)
    volume_size_gauge = Gauge(metric("volume_size"), "Size of volume", volume_labels)
    volume_size_used_gauge = Gauge(metric("volume_size_used"), "Used size of volume", volume_labels)

    share_labels = ["Share_ID", "Share_Name"]
    share_size_used_gauge = Gauge(metric("share_size_used"), "Used size of share", share_labels)
    share_size_quota_gauge = Gauge(metric("share_size_quota"), "Total quota size of share", share_labels)

    disk_labels = ["Disk_ID", "Disk_name", "Disk_model"]
    smart_status_enum = Enum(metric("disk_smart_status"), "Smart status about disk", disk_labels, states=smart_states)
    disk_status_enum = Enum(metric("disk_status_enum"), "Status about disk", disk_labels, states=disk_states)
    disk_temp_gauge = Gauge(metric("disk_temp"), "Temperature of disk", disk_labels)

    print("Metrics are now available at http://localhost:9999/metrics")

    while True:
        try:
            await asyncio.gather(
                api.utilisation.update(),
                api.information.update(),
                api.storage.update(),
                api.share.update(),
            )

        except exceptions.SynologyDSMRequestException as e:
            print( "The Module couldn't reach the Synology API:", e)
            sys.exit(1)

        set_metadata(api, metadata_info)
        set_usage(api, temp_gauge, uptime_gauge, cpu_gauge)
        set_memory(api, memory_used_gauge, memory_total_gauge)
        set_network(api, network_up_gauge, network_down_gauge)
        set_volumes(api, volume_status_enum, volume_size_gauge, volume_size_used_gauge)
        set_disks(api, smart_status_enum, disk_status_enum, disk_temp_gauge)
        set_shares(api, share_size_used_gauge, share_size_quota_gauge)

        sleep(frequency)

if __name__ == "__main__":
    asyncio.run(main())
