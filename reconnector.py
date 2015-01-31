from sparts.tasks.dbus import DBusTask
from sparts.tasks.periodic import PeriodicTask
from sparts.vservice import VService
from sparts.vtask import TryLater
from sparts.sparts import option

import dbus
import time

BUSNAME_NM = 'org.freedesktop.NetworkManager'

IFACE_SETTINGS = 'org.freedesktop.NetworkManager.Settings'
IFACE_CONNECTION = 'org.freedesktop.NetworkManager.Settings.Connection'
IFACE_NM = 'org.freedesktop.NetworkManager'
IFACE_AP = 'org.freedesktop.NetworkManager.AccessPoint'
IFACE_DEVICE = 'org.freedesktop.NetworkManager.Device'
IFACE_DEVICE_WIRELESS = 'org.freedesktop.NetworkManager.Device.Wireless'

PATH_NM = '/org/freedesktop/NetworkManager'
PATH_SETTINGS = '/org/freedesktop/NetworkManager/Settings'

KEY_SETTINGS_WIFI = '802-11-wireless'
KEY_SETTINGS_SSID = 'ssid'
PROP_ACTIVE_AP = 'ActiveAccessPoint'
PROP_SSID = 'Ssid'

SIGNAL_PROPS_CHANGED = 'PropertiesChanged'


def arr_to_str(arr):
    return ''.join(chr(b) for b in arr)


class NMDBusHelper(object):
    """Mix-in with useful helpers for dealing with dbus and NetworkManager"""
    def get_object(self, path):
        return self.sbus.get_object(BUSNAME_NM, path)

    def get_networkmanager(self):
        return self.get_object(PATH_NM)

    def get_settings(self):
        return self.get_object(PATH_SETTINGS)

    def iter_wireless_devices(self):
        nm = self.get_networkmanager()
        for device_path in nm.GetDevices():
            device = self.get_object(device_path)
            try:
                device.GetAll(IFACE_DEVICE_WIRELESS)
                yield device
            except dbus.DBusException:
                # This is not a Wireless Device
                continue

    def iter_wireless_conns(self):
        settings = self.get_settings()
        for conn_path in settings.ListConnections():
            conn = self.get_object(conn_path)
            conn_settings = conn.GetSettings()
            if KEY_SETTINGS_WIFI in conn_settings:
                yield conn

    def get_wireless_conn_by_ssid(self, ssid):
        for conn in self.iter_wireless_conns():
            settings = conn.GetSettings()
            conn_ssid = arr_to_str(
                settings[KEY_SETTINGS_WIFI][KEY_SETTINGS_SSID])
            if conn_ssid == ssid:
                return conn
        raise IndexError("No conn for ssid %s" % (ssid))

    def iter_device_aps(self, device):
        for ap_path in device.GetAccessPoints():
            yield self.get_object(ap_path)

    def get_device_ap_by_ssid(self, device, ssid):
        for ap in self.iter_device_aps(device):
            ap_ssid = self.get_ap_ssid(ap)
            if ap_ssid == ssid:
                return ap
        raise IndexError("No AP for ssid %s" % (ssid))

    def get_device_active_ap(self, device):
        ap_path = device.Get(IFACE_DEVICE_WIRELESS, PROP_ACTIVE_AP)
        return self.get_object(ap_path)

    def get_ap_ssid(self, ap):
        return arr_to_str(ap.Get(IFACE_AP, PROP_SSID))


class NetworkMonitor(DBusTask, NMDBusHelper):
    """Task that watches NetworkManager and the wireless device for signals."""
    
    def initTask(self):
        super(NetworkMonitor, self).initTask()
        self.sbus = dbus.SystemBus(mainloop=self.mainloop_task.dbus_loop)

        nm = self.get_networkmanager()
        self.connect_log_signal(nm, 'CheckPermissions')
        self.connect_log_signal(nm, 'DeviceAdded')
        self.connect_log_signal(nm, 'DeviceRemoved')
        self.connect_log_signal(nm, 'StateChanged')
        self.connect_log_signal(nm, SIGNAL_PROPS_CHANGED)

        settings = self.get_settings()
        self.connect_log_signal(settings, 'NewConnection')
        self.connect_log_signal(settings, SIGNAL_PROPS_CHANGED)

        for device in self.iter_wireless_devices():
            self.connect_log_signal(device, 'AccessPointAdded')
            self.connect_log_signal(device, 'AccessPointRemoved')
            self.connect_log_signal(device, 'ScanDone')
            self.connect_log_signal(device, SIGNAL_PROPS_CHANGED)

    def connect_log_signal(self, obj, name):
        obj.connect_to_signal(
            name,
            self._log_signal,
            sender_keyword='sender',
            destination_keyword='destination',
            interface_keyword='interface',
            member_keyword='member',
            path_keyword='path',
            message_keyword='message',
        )

    def _log_signal(self, *args, **kwargs):
        self.logger.debug('dbus signal received [%s %s]', args, kwargs)


class Reconnector(PeriodicTask, DBusTask, NMDBusHelper):
    OPT_PREFIX = 'reconnect'
    INTERVAL = 5.0
    LOOPLESS = False

    ssid = option(required=True, type=str, metavar='SSID',
                  help='SSID to force reconnection with')

    def initTask(self):
        super(Reconnector, self).initTask()
        self.sbus = dbus.SystemBus(mainloop=self.mainloop_task.dbus_loop)

    def execute(self):
        # Find the connection settings for the SSID
        wanted_conn = self.get_wireless_conn_by_ssid(self.ssid)

        for device in self.iter_wireless_devices():
            # If the active access point is already correct, we're done.
            try:
                active_ap = self.get_device_active_ap(device)
                active_ap_ssid = self.get_ap_ssid(active_ap)
            except dbus.DBusException:
                self.logger.exception("Error getting active AP")
                raise TryLater("Error getting active AP", after=2.5)
            self.logger.debug('Active AP SSID is %s', active_ap_ssid)
            if active_ap_ssid == self.ssid:
                break

            # Find the actual AP for the wanted SSID
            wanted_ap = self.get_device_ap_by_ssid(device, self.ssid)

            # Connect the device, with saved connection settings, to the AP
            t0 = time.time()
            self.logger.info("Connecting to %s...", self.ssid)
            nm = self.get_networkmanager()
            active_connection = nm.ActivateConnection(
                wanted_conn.object_path,
                device.object_path,
                wanted_ap.object_path,
            )
            self.logger.info("Connection took %.2fs", time.time() - t0)
            self.logger.info("result was %s", active_connection)


if __name__ == '__main__':
    NetworkMonitor.register()
    Reconnector.register()
    VService.initFromCLI()
