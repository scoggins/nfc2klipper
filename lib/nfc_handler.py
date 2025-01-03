# SPDX-FileCopyrightText: 2024 Sebastian Andersson <sebastian@bittr.nu>
# SPDX-License-Identifier: GPL-3.0-or-later

""" NFC tag handling """

import time
import logging
import json
from threading import Lock, Event

import ndef
import nfc
from nfc.clf import RemoteTarget


SPOOL = "SPOOL"
FILAMENT = "FILAMENT"
NDEF_TEXT_TYPE = "urn:nfc:wkt:T"
NDEF_JSON_TYPE = "application/json"

logger = logging.getLogger(__name__)

# pylint: disable=R0902
class NfcHandler:
    """NFC Tag handling"""

    def __init__(self, nfc_device: str):
        self.status = ""
        self.nfc_device = nfc_device
        self.on_nfc_no_tag_present = None
        self.on_nfc_tag_present = None
        self.should_stop_event = Event()
        self.write_lock = Lock()
        self.write_event = Event()
        self.write_spool = None
        self.write_filament = None

    @classmethod
    def generate_spool_record(cls, spool, filament, version="1.0"):
        """
        Generate an NFC spool record in JSON format.

        Args:
            spool (str): The spool id from spoolman.
            filament (str): The filament id from spoolman.
            version (str, optional): The version of the protocol. Defaults to "1.0".

        Returns:
            ndef.Record: The NFC record containing the spool and filament information.
        """
        record = {
            'protocol': 'nfc2klipper',
            'version': version,
            'spool': spool,
            'filament': filament
        }

        return ndef.Record(NDEF_JSON_TYPE, name="nfc2klipper", data=json.dumps(record))


    def set_no_tag_present_callback(self, on_nfc_no_tag_present):
        """Sets a callback that will be called when no tag is present"""
        self.on_nfc_no_tag_present = on_nfc_no_tag_present

    def set_tag_present_callback(self, on_nfc_tag_present):
        """Sets a callback that will be called when a tag has been read"""
        self.on_nfc_tag_present = on_nfc_tag_present

    @classmethod
    def get_data_from_ndef_records(cls, records: ndef.TextRecord):
        """Find wanted data from the NDEF records.

        >>> import ndef
        >>> record0 = ndef.TextRecord("")
        >>> record1 = ndef.TextRecord("SPOOL:23\\n")
        >>> record2 = ndef.TextRecord("FILAMENT:14\\n")
        >>> record3 = ndef.TextRecord("SPOOL:23\\nFILAMENT:14\\n")
        >>> NfcHandler.get_data_from_ndef_records([record0])
        (None, None)
        >>> NfcHandler.get_data_from_ndef_records([record3])
        ('23', '14')
        >>> NfcHandler.get_data_from_ndef_records([record1])
        ('23', None)
        >>> NfcHandler.get_data_from_ndef_records([record2])
        (None, '14')
        >>> NfcHandler.get_data_from_ndef_records([record0, record3])
        ('23', '14')
        >>> NfcHandler.get_data_from_ndef_records([record3, record0])
        ('23', '14')
        >>> NfcHandler.get_data_from_ndef_records([record1, record2])
        ('23', '14')
        >>> NfcHandler.get_data_from_ndef_records([record2, record1])
        ('23', '14')
        """

        spool = None
        filament = None

        for record in records:
            # Look for the JSON record first
            if record.type == NDEF_JSON_TYPE and record.name == "nfc2klipper":
                logger.info("Read JSON record: %s", record)
                data = json.loads(record.data)
                spool = data.get("spool")
                filament = data.get("filament")
                break

            # Look for the text record
            if record.type == NDEF_TEXT_TYPE:
                logger.info("Read text record: %s", record)
                for line in record.text.splitlines():
                    line = line.split(":")
                    if len(line) == 2:
                        if line[0] == SPOOL:
                            spool = line[1]
                        if line[0] == FILAMENT:
                            filament = line[1]
                break

            logger.info("Read other record: %s", record)

        return spool, filament

    def write_to_tag(self, spool: int, filament: int) -> bool:
        """Writes spool & filament info to tag. Returns true if worked."""

        self._set_write_info(spool, filament)

        if self.write_event.wait(timeout=30):
            return True

        self._set_write_info(None, None)

        return False

    def run(self):
        """Run the NFC handler, won't return"""
        # Open NFC reader. Will throw an exception if it fails.
        with nfc.ContactlessFrontend(self.nfc_device) as clf:
            while not self.should_stop_event.is_set():
                tag = clf.connect(rdwr={"on-connect": lambda tag: False})
                if tag:
                    self._check_for_write_to_tag(tag)
                    if tag.ndef is None:
                        if self.on_nfc_no_tag_present:
                            self.on_nfc_no_tag_present()
                    else:
                        self._read_from_tag(tag)

                    # Wait for the tag to be removed.
                    while clf.sense(
                        RemoteTarget("106A"), RemoteTarget("106B"), RemoteTarget("212F")
                    ):
                        if self._check_for_write_to_tag(tag):
                            self._read_from_tag(tag)
                        time.sleep(0.2)
                else:
                    time.sleep(0.2)

    def stop(self):
        """Call to stop the handler"""
        self.should_stop_event.set()

    def _write_to_nfc_tag(self, tag, spool: int, filament: int) -> bool:
        """Write given spool/filament ids to the tag"""
        try:
            if tag.ndef and tag.ndef.is_writeable:
                records = []
                for record in tag.ndef.records:
                    # Skip existing JSON record as we'll append it at the end
                    if record.type == NDEF_JSON_TYPE and record.name == "nfc2klipper":
                        continue
                    # Skip the old format, as we're upgrading to the new JSON format
                    if record.type == NDEF_TEXT_TYPE and record.text.find("SPOOL:") != -1:
                        continue
                    records.append(record)
                records.append(NfcHandler.generate_spool_record(spool, filament))
                tag.ndef.records = records

                return True
            self.status = "Tag is write protected"
        except Exception as ex:  # pylint: disable=W0718
            logger.exception(ex)
            self.status = "Got error while writing"
        return False

    def _set_write_info(self, spool, filament):
        if self.write_lock.acquire():  # pylint: disable=R1732
            self.write_spool = spool
            self.write_filament = filament
            self.write_event.clear()
            self.write_lock.release()

    def _check_for_write_to_tag(self, tag) -> bool:
        """Check if the tag should be written to and do it"""
        did_write = False
        if self.write_lock.acquire():  # pylint: disable=R1732
            if self.write_spool:
                if self._write_to_nfc_tag(tag, self.write_spool, self.write_filament):
                    self.write_event.set()
                    did_write = True
                self.write_spool = None
                self.write_filament = None
            self.write_lock.release()
        return did_write

    @classmethod
    def _check_for_needs_update(cls, tag):
        if tag.ndef is None or tag.ndef.records is None:
            return False

        for record in tag.ndef.records:
            if record.type == NDEF_TEXT_TYPE and record.text.find("SPOOL:") != -1:
                return True

        return False

    def _read_from_tag(self, tag):
        """Read data from tag and call callback"""
        if self.on_nfc_tag_present:
            spool, filament = NfcHandler.get_data_from_ndef_records(tag.ndef.records)
            # Check if the tag needs to be updated, if so update it while it's in range.
            if NfcHandler._check_for_needs_update(tag):
                logger.info("Found old tag format, updating to new format")
                self._set_write_info(spool, filament)
            self.on_nfc_tag_present(spool, filament)
