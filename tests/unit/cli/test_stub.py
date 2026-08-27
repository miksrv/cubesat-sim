from cubesat.cli import main as cli


def test_the_console_script_explains_itself_instead_of_crashing(capsys):
    # `pip install -e .` registers `cubesat` as an entry point, so it exists
    # before it is implemented. Exiting with a message beats a traceback.
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "not implemented yet" in err
    assert "cubesat/command" in err
