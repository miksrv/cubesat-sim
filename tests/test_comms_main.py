def test_module_imports_and_exposes_comms_service():
    import src.comms.main as comms_main

    assert comms_main.CommsService.__name__ == "CommsService"
