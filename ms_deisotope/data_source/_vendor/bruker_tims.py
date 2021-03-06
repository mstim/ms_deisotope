'''This interface to the Bruker TDF file format is based upon the TimsData library
that is part of ProteoWizard (Apache II), and a common kernel of Python ctypes functions
found in dia-pasef and other Python wrappers of libtimsdata.
'''
import os
import re
import sqlite3
import sys
import warnings

from ctypes import cdll, c_char_p, c_uint32, c_uint64, c_int64, c_double, c_void_p, POINTER, create_string_buffer
from weakref import WeakValueDictionary

import numpy as np

from ms_peak_picker import reprofile, pick_peaks

from ms_deisotope.utils import Base
from ms_deisotope.data_source.metadata import software
from ms_deisotope.data_source.scan.loader import ScanDataSource
from ms_deisotope.data_source.scan import PrecursorInformation, RawDataArrays
from ms_deisotope.data_source.metadata.activation import ActivationInformation, dissociation_methods
from ms_deisotope.data_source.metadata.scan_traits import IsolationWindow, inverse_reduced_ion_mobility
from ms_deisotope.peak_dependency_network import Interval

if sys.platform[:5] == "win32" or sys.platform[:5] == "win64":
    libname = "timsdata.dll"
elif sys.platform[:5] == "linux":
    libname = "libtimsdata.so"
else:
    raise Exception("Unsupported platform.")


dll = None


def load_library(search_paths=None):
    if search_paths is None:
        search_paths = []
    elif isinstance(search_paths, str):
        search_paths = [search_paths]
    global dll
    if dll is None:
        for lib_path in search_paths:
            try:
                dll = _load_library(lib_path)
            except (Exception) as err:
                continue
            if dll is not None:
                break
    return dll


def _load_library(lib_path):
    dll = cdll.LoadLibrary(os.path.realpath(lib_path))
    dll.tims_open.argtypes = [c_char_p, c_uint32]
    dll.tims_open.restype = c_uint64

    dll.tims_close.argtypes = [c_uint64]
    dll.tims_close.restype = None

    dll.tims_get_last_error_string.argtypes = [c_char_p, c_uint32]
    dll.tims_get_last_error_string.restype = c_uint32

    dll.tims_has_recalibrated_state.argtypes = [c_uint64]
    dll.tims_has_recalibrated_state.restype = c_uint32

    dll.tims_read_scans_v2.argtypes = [
        c_uint64, c_int64, c_uint32, c_uint32, c_void_p, c_uint32]
    dll.tims_read_scans_v2.restype = c_uint32

    convfunc_argtypes = [c_uint64, c_int64, POINTER(
        c_double), POINTER(c_double), c_uint32]

    for fn in [dll.tims_index_to_mz, dll.tims_mz_to_index, dll.tims_scannum_to_oneoverk0,
               dll.tims_oneoverk0_to_scannum, dll.tims_scannum_to_voltage, dll.tims_voltage_to_scannum]:
        fn.argtypes = convfunc_argtypes
        fn.restype = c_uint32
    return dll


def throw_tims_error(dll_handle):
    """Raise the last thronw timsdata error as a
    :exc:`RuntimeError`
    """

    size = dll_handle.tims_get_last_error_string(None, 0)
    buff = create_string_buffer(size)
    dll_handle.tims_get_last_error_string(buff, size)
    raise RuntimeError(buff.value)


def msms_type_to_ms_level(enum):
    if enum == 0:
        return 1
    else:
        return 2


msms_type_to_label = {
    0: "MS1 Scan",
    2: "MS2 Scan",
    8: "PASEF MS2 Scan",
    9: "DIA-PASEF",
}


msms_type_to_metadata_table = {
    2: "FrameMsMsInfo",
    8: "PasefFrameMsMsInfo",
    9: "DiaFrameMsMsWindows"
}


class TIMSMetadata(object):
    def _read_global_metadata(self):
        self._acquisition_parameters = acquisition_parameters = {}
        self._instrument_configuration = instrument_configuration = {}
        self._software = software_map = {
            "acquisition_software": {},
            "control_software": {},
        }
        q = self.conn.execute("SELECT Key, Value FROM GlobalMetadata;")
        for key, value in q:
            if key == "AcquistionSoftware":
                software_map['acquisition_software']['name'] = value
            elif key == "AcquisitionSoftwareVersion":
                software_map['acquisition_software']['version'] = value
            elif key == "InstrmentFamily":
                instrument_configuration['instrument_family'] = value
            elif key == "InstrmentRevision":
                instrument_configuration['instrument_revision'] = value
            elif key == "InstrumentSerialNumber":
                instrument_configuration['serial_number'] = value
            elif key == "AcquisitionDateTime":
                acquisition_parameters['acquisition_date'] = value
            elif key == "OperatorName":
                acquisition_parameters['operator_name'] = value
            elif key == "MzAcqRangeLower":
                acquisition_parameters['scan_window_lower'] = float(value)
            elif key == "MzAcqRangeUpper":
                acquisition_parameters['scan_window_upper'] = float(value)

    def _build_frame_index(self):
        self._frame_counts = {}
        q = self.conn.execute(
            "SELECT MsMsType, Count(*) FROM Frames GROUP BY MsMsType;")
        total = 0
        for scan_type_enum, count in q:
            self._frame_counts[msms_type_to_label[scan_type_enum]] = count
            total += count
        self._frame_counts['Total'] = total

    def _read_metadata(self):
        self._read_global_metadata()
        self._build_frame_index()


class TIMSFrame(Base):
    def __init__(self, source, id, accumulation_time, max_intensity, msms_type, mz_calibration, num_peaks, num_scans,
                 polarity, property_group, ramp_time, scan_mode, summed_intensities, t1, t2, time, tims_calibration,
                 tims_id, pasef_precursors=None):
        if pasef_precursors is None:
            pasef_precursors = []
        self.source = source
        self.id = id
        self.accumulation_time = accumulation_time
        self.max_intensity = max_intensity
        self.msms_type = msms_type
        self.mz_calibration = mz_calibration
        self.num_peaks = num_peaks
        self.num_scans = num_scans
        self.polarity = polarity
        self.property_group = property_group
        self.ramp_time = ramp_time
        self.scan_mode = scan_mode
        self.summed_intensities = summed_intensities
        self.t1 = t1
        self.t2 = t2
        self.time = time
        self.tims_calibration = tims_calibration
        self.tims_id = tims_id
        self.pasef_precursors = pasef_precursors

    @classmethod
    def from_query(cls, source, rowdict):
        frame = cls(
            source,
            rowdict['Id'],
            rowdict['AccumulationTime'],
            rowdict['MaxIntensity'],
            rowdict['MsMsType'],
            rowdict['MzCalibration'],
            rowdict['NumPeaks'],
            rowdict['NumScans'],
            rowdict['Polarity'],
            rowdict['PropertyGroup'],
            rowdict['RampTime'],
            rowdict['ScanMode'],
            rowdict['SummedIntensities'],
            rowdict['T1'],
            rowdict['T2'],
            rowdict['Time'],
            rowdict['TimsCalibration'],
            rowdict['TimsId'],
        )
        return frame


class PASEFPrecursorInformation(Base):
    def __init__(self, frame_id, start_scan, end_scan, isolation_mz, isolation_width, collision_energy, monoisotopic_mz,
                 charge, average_scan_number, intensity, parent):
        self.frame_id = frame_id
        self.start_scan = start_scan
        self.end_scan = end_scan
        self.isolation_mz = isolation_mz
        self.isolation_width = isolation_width
        self.collision_energy = collision_energy
        self.monoisotopic_mz = monoisotopic_mz
        self.charge = charge
        self.average_scan_number = average_scan_number
        self.intensity = intensity
        self.parent = parent

    @classmethod
    def from_query(cls, rowdict):
        pasef_pinfo = cls(
            rowdict['Frame'],
            rowdict['ScanNumBegin'],
            rowdict['ScanNumEnd'],
            rowdict['IsolationMz'],
            rowdict['IsolationWidth'],
            rowdict['CollisionEnergy'],
            rowdict['MonoisotopicMz'],
            rowdict['Charge'],
            rowdict['ScanNumber'],
            rowdict['Intensity'],
            rowdict['Parent']
        )
        return pasef_pinfo


class TIMSSpectrumDataBase(Base):
    def __init__(self, frame, start_scan, end_scan=None):
        self.frame = frame
        self.start_scan = start_scan
        self._end_scan = end_scan

    @property
    def end_scan(self):
        if self._end_scan is None:
            return self.start_scan + 1
        else:
            return self._end_scan

    def is_combined(self):
        if self._end_scan is not None and self._end_scan - self.start_scan > 1:
            return True
        return False

    def make_id_string(self):
        if self._end_scan is None:
            return "frame=%d scan=%d" % (self.frame.id, self.start_scan + 1)
        else:
            return "frame=%d startScan=%d endScan=%d" % (self.frame.id, self.start_scan + 1, self.end_scan + 1)


class TIMSPASEFSpectrumData(TIMSSpectrumDataBase):
    def __init__(self, frame, start_scan, pasef_precursor, end_scan=None):
        super(TIMSPASEFSpectrumData, self).__init__(
            frame, start_scan, end_scan)
        self.pasef_precursor = pasef_precursor


default_scan_merging_parameters = {
    "fwhm": 0.04,
    "dx": 0.001
}


class TIMSScanDataSource(ScanDataSource):
    _scan_merging_parameters = default_scan_merging_parameters.copy()

    def _is_profile(self, scan):
        if scan.is_combined():
            return True
        return False

    def _scan_time(self, scan):
        return scan.frame.time

    def _scan_id(self, scan):
        return scan.make_id_string()

    def _scan_title(self, scan):
        return self._scan_id(scan)

    def _ms_level(self, scan):
        if scan.frame.msms_type == 0:
            return 1
        return 2

    def _polarity(self, scan):
        if scan.frame.polarity == "+":
            return 1
        elif scan.frame.polarity == '-':
            return -1
        else:
            return None

    def _activation(self, scan):
        mode = scan.frame.scan_mode
        if mode in (2, 8, 9):
            method = dissociation_methods["collision-induced dissociation"]
        elif mode in (3, 4, 5):
            method = dissociation_methods['in-source collision-induced dissociation']
        else:
            print("Unknown Scan Mode %d, Unknown Dissociation. Returning CID" % (mode, ))
            method = dissociation_methods["collision-induced dissociation"]
        precursor = self._locate_pasef_precursor_for(scan)
        if precursor is not None:
            collision_energy = precursor.collision_energy
        return ActivationInformation(method, collision_energy)

    def _locate_pasef_precursor_for(self, scan):
        if scan.is_combined():
            # raise ValueError("Cannot determine precursor for combined spectra yet")
            query_interval = Interval(scan.start_scan, scan.end_scan)
            matches = []
            for precursor in scan.frame.pasef_precursors:
                if query_interval.overlaps(Interval(precursor.start_scan, precursor.end_scan)):
                    matches.append(precursor)
            n_matches = len(matches)
            if n_matches == 0:
                return None
            elif n_matches > 1:
                raise ValueError("Multiple precursors found for scan interval!")
            else:
                return matches[0]

        else:
            scan_number = scan.start_scan
            for precursor in scan.frame.pasef_precursors:
                if precursor.start_scan <= scan_number < precursor.end_scan:
                    return precursor
            return None

    def _precursor_information(self, scan):
        if scan.frame.msms_type == 8:
            precursor = self._locate_pasef_precursor_for(scan)
            if precursor is not None:
                mz = precursor.monoisotopic_mz
                intensity = precursor.intensity
                charge = precursor.charge
                parent_frame_id = precursor.parent

                if not scan.is_combined():
                    current_drift_time = self.scan_number_to_one_over_K0(scan.frame.id, [scan.start_scan])
                else:
                    current_drift_time = self.scan_number_to_one_over_K0(
                        scan.frame.id, [np.ceil(precursor.average_scan_number)])

                parent_frame = self.get_frame_by_id(parent_frame_id)
                precursor_scan_id = TIMSPASEFSpectrumData(parent_frame, precursor.start_scan, None, precursor.end_scan).make_id_string()
                product_scan_id = scan.make_id_string()
                pinfo = PrecursorInformation(
                    mz, intensity, charge, precursor_scan_id,
                    product_scan_id=product_scan_id, source=self, annotations={
                        inverse_reduced_ion_mobility: current_drift_time,
                    })
                return pinfo
        return None

    def _isolation_window(self, scan):
        if scan.frame.msms_type == 8:
            precursor = self._locate_pasef_precursor_for(scan)
            if precursor is not None:
                width = precursor.isolation_width / 2
                window = IsolationWindow(width, precursor.isolation_mz, width)
                return window
        return None

    def _scan_index(self, scan):
        cursor = self.conn.execute("SELECT sum(NumScans) FROM Frames WHERE Id < ?", (scan.frame.id, ))
        result = cursor.fetchone()[0]
        if result is None:
            result = 0
        return result + scan.start_scan

    def _acquisition_informatioN(self, scan):
        pass

    def _get_centroids(self, scan):
        mzs, intensities = self.read_spectrum(
            scan.frame.id, scan.start_scan, scan.end_scan)
        sort_mask = np.argsort(mzs)
        mzs = mzs[sort_mask]
        intensities = intensities[sort_mask]
        centroids = pick_peaks(mzs, intensities, peak_mode="centroid")
        return centroids

    def _scan_arrays(self, scan):
        if scan.is_combined():
            mzs, intensities = self.read_spectrum(
                scan.frame.id, scan.start_scan, scan.end_scan)
            sort_mask = np.argsort(mzs)
            mzs = mzs[sort_mask]
            intensities = intensities[sort_mask]
            centroids = pick_peaks(mzs, intensities, peak_mode="centroid")
            mzs, intensities = reprofile(
                centroids, dx=self._scan_merging_parameters['dx'],
                override_fwhm=self._scan_merging_parameters['fwhm'])
            return mzs, intensities
        else:
            mzs, intensities = self.read_spectrum(scan.frame.id, scan.start_scan, scan.end_scan)
            return mzs, intensities


single_scan_id_parser = re.compile(r"frame=(\d+) scan=(\d+)")
multi_scan_id_parser = re.compile(r"frame=(\d+) startScan=(\d+) endScan=(\d+)")


class TIMSData(TIMSMetadata, TIMSScanDataSource):

    def __init__(self, analysis_directory, use_recalibrated_state=False, scan_merging_parameters=None):
        if sys.version_info.major == 2:
            if not isinstance(analysis_directory, unicode):
                raise ValueError("analysis_directory must be a Unicode string.")
        if sys.version_info.major == 3:
            if not isinstance(analysis_directory, str):
                raise ValueError("analysis_directory must be a string.")
        if scan_merging_parameters is None:
            scan_merging_parameters = default_scan_merging_parameters.copy()
        else:
            for key, value in default_scan_merging_parameters.items():
                scan_merging_parameters.setdefault(key, value)

        self.dll = load_library()

        self.handle = self.dll.tims_open(
            analysis_directory.encode('utf-8'), 1 if use_recalibrated_state else 0)
        if self.handle == 0:
            throw_tims_error(self.dll)

        self.conn = sqlite3.connect(os.path.join(analysis_directory, "analysis.tdf"))
        self.conn.row_factory = sqlite3.Row

        self.initial_frame_buffer_size = 128 # may grow in readScans()
        self._read_metadata()
        self._frame_cache = WeakValueDictionary()
        self._scan_merging_parameters = scan_merging_parameters


    def __del__(self):
        if hasattr(self, 'handle'):
            self.dll.tims_close(self.handle)

    def _describe_frame(self, frame_id):
        cursor = self.conn.execute("SELECT * FROM Frames WHERE Id={0};".format(frame_id))
        return dict(cursor.fetchone())

    def get_scan_by_id(self, scan_id):
        match = single_scan_id_parser.match(scan_id)
        if match is None:
            match = multi_scan_id_parser.match(scan_id)
            if match is None:
                raise ValueError("%r does not look like a TIMS nativeID" % (scan_id, ))
            else:
                frame_id, start_scan, end_scan = map(int, match.groups())
                frame = self.get_frame_by_id(frame_id)
                if frame.msms_type == 8:
                    pasef_scan = TIMSPASEFSpectrumData(
                        frame, start_scan - 1, None, end_scan)
                    pasef_scan.pasef_precursor = self._locate_pasef_precursor_for(pasef_scan)
                    scan_obj = self._make_scan(pasef_scan)
                else:
                    scan_obj = self._make_scan(TIMSSpectrumDataBase(frame, start_scan - 1, end_scan))
        else:
            frame_id, scan_number = map(int, match.groups())
            frame = self.get_frame_by_id(frame_id)
            if frame.msms_type == 8:
                pasef_scan = TIMSPASEFSpectrumData(frame, scan_number - 1, None)
                pasef_scan.pasef_precursor = self._locate_pasef_precursor_for(pasef_scan)
                scan_obj = self._make_scan(pasef_scan)
            else:
                scan_obj = self._make_scan(TIMSSpectrumDataBase(frame, scan_number - 1))
        # Cache scan here
        return scan_obj

    def _convert_callback(self, frame_id, input_data, func):
        if isinstance(input_data, np.ndarray) and input_data.dtype == np.float64:
            # already supports buffer protocol, no extra copy
            in_array = input_data
        else:
            # convert data to appropriate float data buffer
            in_array = np.array(input_data, dtype=np.float64)
        cnt = len(in_array)
        out = np.empty(shape=cnt, dtype=np.float64)
        success = func(self.handle, frame_id,
                       in_array.ctypes.data_as(POINTER(c_double)),
                       out.ctypes.data_as(POINTER(c_double)),
                       cnt)
        if success == 0:
            throw_tims_error(self.dll)
        return out

    def index_to_mz(self, frame_id, mzs):
        return self._convert_callback(frame_id, mzs, self.dll.tims_index_to_mz)

    def mz_to_index(self, frame_id, mzs):
        return self._convert_callback(frame_id, mzs, self.dll.tims_mz_to_index)

    def scan_number_to_one_over_K0(self, frame_id, mzs):
        return self._convert_callback(frame_id, mzs, self.dll.tims_scannum_to_oneoverk0)

    def one_over_K0_to_scan_number(self, frame_id, mzs):
        return self._convert_callback(frame_id, mzs, self.dll.tims_oneoverk0_to_scannum)

    def scan_number_to_voltage(self, frame_id, mzs):
        return self._convert_callback(frame_id, mzs, self.dll.tims_scannum_to_voltage)

    def voltage_to_scan_number(self, frame_id, mzs):
        return self._convert_callback(frame_id, mzs, self.dll.tims_voltage_to_scannum)

    def get_frame_by_id(self, frame_id):
        if frame_id in self._frame_cache:
            return self._frame_cache[frame_id]
        cursor = self.conn.execute("SELECT * FROM Frames WHERE Id={0};".format(frame_id))
        frame = TIMSFrame.from_query(self, dict(cursor.fetchone()))
        # MS1
        if frame.msms_type == 0:
            pass
        # PASEF MS2
        elif frame.msms_type == 8:
            pasef_cursor = self.conn.execute(
                """SELECT Frame, ScanNumBegin, ScanNumEnd, IsolationMz, IsolationWidth, CollisionEnergy, MonoisotopicMz,
                          Charge, ScanNumber, Intensity, Parent FROM PasefFrameMsMsInfo f JOIN Precursors p on p.id=f.precursor
                          WHERE Frame = ?
                          ORDER BY ScanNumBegin
                          """, (frame_id, ))
            frame.pasef_precursors.extend(map(PASEFPrecursorInformation.from_query, pasef_cursor))
        else:
            warnings.warn("No support for MSMSType %r yet" % (frame.msms_type, ))
        self._frame_cache[frame_id] = frame
        return frame

    # Output: list of tuples (indices, intensities)
    def read_scans(self, frame_id, scan_begin, scan_end):
        # buffer-growing loop
        while True:
            cnt = int(self.initial_frame_buffer_size)
            buf = np.empty(shape=cnt, dtype=np.uint32)
            buffer_size_in_bytes = 4 * cnt

            required_len = self.dll.tims_read_scans_v2(self.handle, frame_id, scan_begin, scan_end,
                                                       buf.ctypes.data_as(POINTER(c_uint32)),
                                                       buffer_size_in_bytes)
            if required_len == 0:
                throw_tims_error(self.dll)

            if required_len > buffer_size_in_bytes:
                if required_len > 16777216:
                    # arbitrary limit for now...
                    raise RuntimeError("Maximum expected frame size exceeded.")
                self.initial_frame_buffer_size = required_len / 4 + 1 # grow buffer
            else:
                break

        result = []
        d = scan_end - scan_begin
        for i in range(scan_begin, scan_end):
            npeaks = buf[i-scan_begin]
            indices = buf[d:d + npeaks]
            d += npeaks
            intensities = buf[d:d + npeaks]
            d += npeaks
            result.append((indices, intensities))

        return result

    def read_spectrum(self, frame_id, scan_begin, scan_end):
        scans = self.read_scans(frame_id, scan_begin, scan_end)
        # Summarize on a grid
        allind = []
        allint = np.array([], dtype=float)
        for scan in scans:
            indices = np.array(scan[0])
            if len(indices) > 0:
                intens = scan[1]
                allind = np.concatenate((allind, indices))
                allint = np.concatenate((allint, intens))
        allmz = self.index_to_mz(frame_id, allind)
        return allmz, allint
