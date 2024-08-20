
from pathlib import Path
import shutil
from openccm import run, ConfigParser
from opencmp.run import run as run_opencmp
from opencmp.config_functions import ConfigParser as OpenCMPConfigParser

rel_path_to_examples = '../examples/'


def clean_previous_results(configparser: ConfigParser) -> None:
    def delete_and_recreate_folder(path: str) -> None:
        if Path(path).exists():
            shutil.rmtree(path)
            Path(path).mkdir(parents=True)

    if configparser.need_to_update_paths:
        configparser.update_paths()

    delete_and_recreate_folder(configparser['SETUP']['tmp_folder_path'])
    delete_and_recreate_folder(configparser['SETUP']['log_folder_path'])
    delete_and_recreate_folder(configparser['SETUP']['output_folder_path'])


def clean_and_run(working_directory: str) -> None:
    configparser = ConfigParser(working_directory + 'CONFIG')
    configparser['SETUP']['working_directory'] = working_directory

    clean_previous_results(configparser)
    run(configparser)


def test_opencmp_cstr_reversible():
    clean_and_run(rel_path_to_examples + 'simple_reactors/cstr/reversible/')


def test_opencmp_cstr_irreversible():
    clean_and_run(rel_path_to_examples + 'simple_reactors/cstr/irreversible/')


def test_opencmp_pfr():
    clean_and_run(rel_path_to_examples + 'simple_reactors/pfr/')


def test_opencmp_recirc():
    path_to_folder = rel_path_to_examples + 'OpenCMP/pipe_with_recirc_2d/'

    configparser_ccm = ConfigParser(path_to_folder + 'CONFIG')
    configparser_ccm['SETUP']['working_directory'] = path_to_folder

    if not Path(path_to_folder + configparser_ccm['INPUT']['opencmp_sol_file_path']).exists():
        configparser = OpenCMPConfigParser(path_to_folder + 'config_Stokes')
        configparser['MESH']['filename'] = path_to_folder + configparser['MESH']['filename']
        configparser['OTHER']['run_dir'] = path_to_folder
        run_opencmp("", configparser)

        configparser = OpenCMPConfigParser(path_to_folder + 'config_INS')
        configparser['MESH']['filename'] = path_to_folder + configparser['MESH']['filename']
        configparser['OTHER']['run_dir'] = path_to_folder
        run_opencmp("", configparser)

    clean_and_run(path_to_folder)


def test_openfoam_2d_pipe():
    clean_and_run(rel_path_to_examples + 'OpenFOAM/pipe_with_recirc/')
