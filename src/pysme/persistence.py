import io
import logging
from zipfile import ZipFile
import json
import tempfile
import subprocess


import numpy as np

logger = logging.getLogger(__name__)

# Update this if the names in sme change
updates = {"idlver": "system_info"}


def toBaseType(value):
    if value is None:
        return value
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.str):
        return str(value)

    return value


def save(filename, data, folder=""):
    """
    Create a folder structure inside a zipfile
    Add .json and .npy and .npz files with the correct names
    And subfolders for more complicated objects
    with the same layout
    Each class should have a save and a load method
    which can be used for this purpose

    Parameters
    ----------
    filename : str
        Filename of the final zipfile
    data : SME_struct
        data to save
    """
    with ZipFile(filename, "w") as file:
        saves(file, data, folder=folder)


def saves(file, data, folder=""):
    if folder != "" and folder[-1] != "/":
        folder = folder + "/"

    parameters = {}
    arrays = {}
    others = {}
    for key in data._names:
        value = getattr(data, key)
        if np.isscalar(value) or isinstance(value, dict):
            parameters[key] = value
        elif isinstance(value, (list, np.ndarray)):
            if np.size(value) > 20:
                arrays[key] = value
            else:
                parameters[key] = value
        else:
            others[key] = value

    info = json.dumps(parameters, default=toBaseType)
    file.writestr(f"{folder}info.json", info)

    for key, value in arrays.items():
        b = io.BytesIO()
        np.save(b, value)
        file.writestr(f"{folder}{key}.npy", b.getvalue())

    for key, value in others.items():
        if value is not None:
            value._save(file, f"{folder}{key}")


def load(filename, data):
    with ZipFile(filename, "r") as file:
        names = file.namelist()
        return loads(file, data, names)


def loads(file, data, names=None, folder=""):
    if folder != "" and folder[-1] != "/":
        folder = folder + "/"
    if names is None:
        names = file.namelist()

    subdirs = {}
    local = []
    for name in names:
        name_within = name[len(folder) :]
        if "/" not in name_within:
            local.append(name)
        else:
            direc, _ = name_within.split("/", 1)
            if direc not in subdirs.keys():
                subdirs[direc] = []
            subdirs[direc].append(name)

    for name in local:
        if name.endswith(".json"):
            info = file.read(name)
            info = json.loads(info)
            for key, value in info.items():
                key = updates.get(key, key)
                data[key] = value
        elif name.endswith(".npy") or name.endswith(".npz"):
            b = io.BytesIO(file.read(name))
            key = name[len(folder) : -4]
            key = updates.get(key, key)
            data[key] = np.load(b)

    for key, value in subdirs.items():
        data_key = updates.get(key, key)
        data[data_key] = data[data_key]._load(file, value, folder=folder + key)

    return data


def get_typecode(dtype):
    """ Get the IDL typecode for a given dtype """
    if dtype.name[:5] == "bytes":
        return "1"
    if dtype.name == "int16":
        return "2"
    if dtype.name == "int32":
        return "3"
    if dtype.name == "float32":
        return "4"
    if dtype.name == "float64":
        return "5"
    if dtype.name[:3] == "str":
        return dtype.name[3:]


temps_to_clean = []


def save_as_binary(arr):
    global temps_to_clean

    with tempfile.NamedTemporaryFile("w+", suffix=".dat", delete=False) as temp:
        if arr.dtype.name[:3] == "str" or arr.dtype.name == "object":
            arr = arr.astype(bytes)
            shape = (arr.dtype.itemsize, len(arr))
        else:
            shape = arr.shape[::-1]

        arr.tofile(temp)
        value = [
            temp.name,
            str(list(shape)),
            get_typecode(arr.dtype),
        ]
    temps_to_clean += [temp]
    return value


def clean_temps():
    global temps_to_clean
    for temp in temps_to_clean:
        try:
            os.remove(temp)
        except:
            pass

    temps_to_clean = []


def write_as_idl(sme):
    """
    Write SME structure into and idl format 
    data arrays are stored in seperate temp files, and only the filename is passed to idl
    """

    wind = np.cumsum(sme.wave.shape[1]) + 1

    idl_fields = {
        "version": float(sme.version),
        "id": sme.id,
        "teff": sme.teff,
        "grav": sme.logg,
        "feh": sme.monh,
        "vmic": sme.vmic,
        "vmac": sme.vmac,
        "vsini": sme.vsini,
        "vrad": sme.vrad.tolist(),
        "vrad_flag": {"none": -2, "whole": -1, "each": 0}[sme.vrad_flag],
        "cscale": sme.cscale.tolist(),
        "cscale_flag": {
            "none": -3,
            "fix": -2,
            "constant": 0,
            "linear": 1,
            "quadratic": 1,
        }[sme.cscale_flag],
        "gam6": sme.gam6,
        "h2broad": int(sme.h2broad),
        "accwi": sme.accwi,
        "accrt": sme.accrt,
        "clim": 0.01,
        "maxiter": sme.fitresults.maxiter,
        "chirat": sme.fitresults.chisq,
        "nmu": sme.nmu,
        "nseg": sme.nseg,
        "abund": save_as_binary(sme.abund.get_pattern(raw=True)),
        "species": save_as_binary(sme.species),
        "atomic": save_as_binary(sme.atomic),
        "lande": save_as_binary(sme.linelist.lande),
        "depth": save_as_binary(sme.linelist.depth),
        "lineref": save_as_binary(sme.linelist.reference),
        "short_line_format": {"short": 1, "long": 2}[sme.linelist.lineformat],
        "wran": sme.wran.tolist(),
        "mu": sme.mu.tolist(),
        "wave": save_as_binary(sme.wave.ravel()),
        "wind": wind.tolist(),
        "sob": save_as_binary(sme.spec.ravel()),
        "uob": save_as_binary(sme.uncs.ravel()),
        "mob": save_as_binary(sme.mask.ravel()),
        "obs_name": "",
        "obs_type": "",
        "iptype": sme.iptype,
        "ipres": sme.ipres,
        # "ip_x": sme.ip_x,
        # "ip_y": sme.ip_y,
        "atmo": {
            "method": sme.atmo.method,
            "source": sme.atmo.source,
            "depth": sme.atmo.depth,
            "interp": sme.atmo.interp,
            "geom": sme.atmo.geom,
        },
    }

    if sme.synth is not None:
        idl_fields["smod"] = save_as_binary(sme.synth.ravel())

    if sme.linelist.lineformat == "long":
        idl_fields.update(
            {
                "line_extra": save_as_binary(sme.linelist.extra),
                "line_lulande": save_as_binary(sme.linelist.lulande),
                "line_term_low": save_as_binary(sme.linelist.term_low),
                "line_term_upp": save_as_binary(sme.linelist.term_upp),
            }
        )

    sep = ""
    text = ""

    for key, value in idl_fields.items():
        if isinstance(value, dict):
            text += f"{sep}{key!s}:{{$\n"
            sep = ""
            for key2, value2 in value.items():
                text += f"{sep}{key2!s}:{value2!r}$\n"
                sep = ","
            sep = ","
            text += "}$\n"
        else:
            text += f"{sep}{key!s}:{value!r}$\n"
            sep = ","
    return text


def save_as_idl(sme, fname):
    """
    Save the SME structure to disk as an idl save file

    This writes a IDL script to a temporary file, which is then run
    with idl as a seperate process. Therefore this reqires a working
    idl installation.

    There are two steps to this. First all the fields from the sme,
    structure need to be transformed into simple idl readable structures.
    All large arrays are stored in seperate binary files, for performance.
    The script then reads those files back into idl.
    """
    with tempfile.NamedTemporaryFile("w+", suffix=".pro") as temp:
        tempname = temp.name
        temp.write("sme = {")
        # TODO: Save data as idl compatible data
        temp.write(write_as_idl(sme))
        temp.write("} \n")
        # This is the code that will be run in idl
        temp.write(
            """tags = tag_names(sme)
new_sme = {}

for i = 0, n_elements(tags)-1 do begin
    arr = sme.(i)
    s = size(arr)
    if (s[0] eq 1) and (s[1] eq 3) then begin
        void = execute('shape = ' + arr[1])
        type = fix(arr[2])
        arr = read_binary(arr[0], data_dims=shape, data_type=type, endian='big')
        if type eq 1 then begin
            ;string
            arr = string(arr)
        endif
    endif
    if (s[s[0]+1] eq 8) then begin
        ;struct
        tags2 = tag_names(sme.(i))
        new2 = {}
        tmp = sme.(i)

        for j = 0, n_elements(tags2)-1 do begin
            arr2 = tmp.(j)
            s = size(arr2)
            if (s[0] eq 1) and (s[1] eq 3) then begin
                void = execute('shape = ' + arr2[1])
                type = fix(arr2[2])
                arr2 = read_binary(arr2[0], data_dims=shape, data_type=type, endian='big')
                if type eq 1 then begin
                    ;string
                    arr2 = string(arr2)
                endif
            endif
            new2 = create_struct(temporary(new2), tags2[j], arr2)
        endfor
        arr = new2
    endif
    new_sme = create_struct(temporary(new_sme), tags[i], arr)
endfor

sme = new_sme\n"""
        )
        temp.write(f'save, sme, filename="{fname}"\n')
        temp.write("end\n")
        temp.flush()

        # with open(os.devnull, 'w') as devnull:
        subprocess.run(["idl", "-e", ".r %s" % tempname])
        clean_temps()


class IPersist:
    def _save(self, file, folder=""):
        saves(file, self, folder)

    @classmethod
    def _load(cls, file, names, folder=""):
        logger.setLevel(logging.INFO)
        data = cls()  # TODO Suppress warnings
        data = loads(file, data, names, folder)
        logger.setLevel(logging.NOTSET)
        return data
