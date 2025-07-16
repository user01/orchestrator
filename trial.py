import pdb
import time


def main():
    print("started")
    time.sleep(0.02)
    for i in range(20):
        print(f"pre: {i}")

    for i in range(2):
        print(f"started: {i}")
        import pdb; pdb.set_trace()
        time.sleep(0.05)
    print("done")
    pass

if __name__ == "__main__":
    main()
