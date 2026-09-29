from brokers.dhan.client  import DhanClient


def main() -> None:
    print("========================================")
    print("       TRADEGO - DHAN API TEST")
    print("========================================")
    print("Connecting to DhanHQ...")
    print()

    try:
        dhan = DhanClient()
        profile = dhan.get_profile()

        print("HTTP Status: 200")
        print()
        print("✅ DHAN API CONNECTION SUCCESSFUL")
        print("----------------------------------------")
        print(f"Client ID      : {profile.get('dhanClientId')}")
        print(f"Token Validity : {profile.get('tokenValidity')}")
        print(f"Active Segment : {profile.get('activeSegment')}")
        print(f"DDPI           : {profile.get('ddpi')}")
        print(f"MTF            : {profile.get('mtf')}")
        print(f"Data Plan      : {profile.get('dataPlan')}")
        print(f"Data Validity  : {profile.get('dataValidity')}")
        print("----------------------------------------")

    except Exception as error:
        print()
        print("❌ DHAN API CONNECTION FAILED")
        print("----------------------------------------")
        print(type(error).__name__)
        print(error)
        print("----------------------------------------")


if __name__ == "__main__":
    main()